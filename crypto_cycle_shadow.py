"""V6 crypto strategy research ledger.

This module is deliberately shadow-only.  It consumes candidates that the live
scanner has already priced, records hypothetical positions, and settles them
from finalized Kalshi markets.  It never imports an order client or mutates the
live portfolio.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from crypto_pricing import kalshi_order_fee


VERSION = "crypto-cycle-shadow-v6"
MODE = "paper_shadow_only"
COMPATIBLE_VERSIONS = {
    "crypto-cycle-shadow-v1",
    "crypto-cycle-shadow-v2",
    "crypto-cycle-shadow-v3",
    "crypto-cycle-shadow-v4",
    "crypto-cycle-shadow-v5",
    VERSION,
}


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _boolean(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _now(value=None):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        epoch = float(value)
        if abs(epoch) >= 1_000_000_000_000:
            epoch /= 1000.0
        return datetime.fromtimestamp(epoch, tz=timezone.utc)
    if value:
        try:
            numeric = float(value)
            if math.isfinite(numeric) and str(value).strip().replace(".", "", 1).isdigit():
                if abs(numeric) >= 1_000_000_000_000:
                    numeric /= 1000.0
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (TypeError, ValueError, OSError, OverflowError):
            pass
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc)


def _iso(value=None):
    return _now(value).isoformat()


def _setting(settings, key, default):
    return (settings or {}).get(key, default)


def _rounded(value, digits=4):
    return round(_number(value), digits)


def configuration(settings=None):
    settings = settings or {}
    allowed = str(
        _setting(settings, "CRYPTO_CYCLE_SHADOW_ALLOWED_ASSETS", "BTC,ETH,SOL,DOGE")
    )
    config = {
        "enabled": _boolean(_setting(settings, "CRYPTO_CYCLE_SHADOW_ENABLED", True), True),
        "bot_count": max(1, _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_BOT_COUNT", 3), 3)),
        "cycle_count": max(1, _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_CYCLE_COUNT", 100), 100)),
        "research_goal_markets": max(
            1,
            _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_RESEARCH_GOAL_MARKETS", 100), 100),
        ),
        "cycle_goal_dollars": max(
            0.01,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_CYCLE_GOAL_DOLLARS", 0.25), 0.25),
        ),
        "max_attempts": max(1, _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_MAX_ATTEMPTS", 4), 4)),
        "max_contracts": max(1, _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_MAX_CONTRACTS", 3), 3)),
        "max_attempt_cost_dollars": max(
            0.01,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_MAX_ATTEMPT_COST_DOLLARS", 1.50), 1.50),
        ),
        "max_cycle_loss_dollars": max(
            0.01,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_MAX_CYCLE_LOSS_DOLLARS", 3.0), 3.0),
        ),
        "starting_bankroll_per_bot": max(
            0.0,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_STARTING_BANKROLL_PER_BOT", 250), 250),
        ),
        "allowed_assets": sorted({item.strip().upper() for item in allowed.split(",") if item.strip()}),
        "early_minimum_minutes": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MIN_MINUTES", 10.0), 10.0
        ),
        "early_maximum_minutes": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MAX_MINUTES", 13.0), 13.0
        ),
        "early_minimum_price_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MIN_PRICE_CENTS", 35.0), 35.0
        ),
        "early_maximum_price_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MAX_PRICE_CENTS", 44.0), 44.0
        ),
        "early_minimum_model_advantage_pp": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MIN_MODEL_ADVANTAGE_PP", 0.25), 0.25
        ),
        "early_minimum_opposing_sources": max(
            1,
            _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MIN_OPPOSING_SOURCES", 2), 2),
        ),
        "early_minimum_data_quality": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MIN_DATA_QUALITY", 0.96), 0.96
        ),
        "early_maximum_spread_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MAX_SPREAD_CENTS", 3.0), 3.0
        ),
        "early_maximum_quote_age_seconds": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_EARLY_MAX_QUOTE_AGE_SECONDS", 2.0), 2.0
        ),
        "minimum_flow_source_strength": max(
            0.0,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_MIN_FLOW_SOURCE_STRENGTH", 0.02), 0.02),
        ),
        "late_minimum_minutes": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MIN_MINUTES", 1.75), 1.75
        ),
        "late_maximum_minutes": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MAX_MINUTES", 2.50), 2.50
        ),
        "late_minimum_price_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MIN_PRICE_CENTS", 10.0), 10.0
        ),
        "late_maximum_price_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MAX_PRICE_CENTS", 39.0), 39.0
        ),
        "late_minimum_data_quality": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MIN_DATA_QUALITY", 0.90), 0.90
        ),
        "late_maximum_spread_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MAX_SPREAD_CENTS", 4.0), 4.0
        ),
        "late_maximum_quote_age_seconds": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MAX_QUOTE_AGE_SECONDS", 2.0), 2.0
        ),
        "late_maximum_expected_slippage_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_LATE_MAX_SLIPPAGE_CENTS", 3.0), 3.0
        ),
        "max_same_expiry_open": 1,
        "maker_counterfactual_enabled": _boolean(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_MAKER_COUNTERFACTUAL_ENABLED", True), True
        ),
        "maker_minimum_spread_cents": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_MAKER_MIN_SPREAD_CENTS", 2.0), 2.0
        ),
        "maker_cancel_minutes": _number(
            _setting(settings, "CRYPTO_CYCLE_SHADOW_MAKER_CANCEL_MINUTES", 8.5), 8.5
        ),
        "edge_discovery_enabled": _boolean(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_SHADOW_ENABLED", True), True
        ),
        "edge_discovery_minimum_minutes": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_MINUTES", 2.0), 2.0
        ),
        "edge_discovery_maximum_minutes": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MAX_MINUTES", 13.0), 13.0
        ),
        "edge_discovery_minimum_price_cents": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_PRICE_CENTS", 10.0), 10.0
        ),
        "edge_discovery_maximum_price_cents": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MAX_PRICE_CENTS", 80.0), 80.0
        ),
        "edge_discovery_minimum_raw_edge_cents": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_RAW_EDGE_CENTS", 1.0), 1.0
        ),
        "edge_discovery_minimum_raw_edge_low_cents": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_RAW_EDGE_LOW_CENTS", -1.5), -1.5
        ),
        "edge_discovery_maximum_raw_market_gap_pp": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MAX_RAW_MARKET_GAP_PP", 6.0), 6.0
        ),
        "edge_discovery_maximum_residual_correction_pp": max(
            0.0,
            _number(
                _setting(
                    settings,
                    "CRYPTO_EDGE_DISCOVERY_MAX_RESIDUAL_CORRECTION_PP",
                    5.0,
                ),
                5.0,
            ),
        ),
        "edge_discovery_minimum_target_sigma": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_TARGET_SIGMA", -0.50), -0.50
        ),
        "edge_discovery_minimum_data_quality": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_DATA_QUALITY", 0.96), 0.96
        ),
        "edge_discovery_maximum_spread_cents": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MAX_SPREAD_CENTS", 3.0), 3.0
        ),
        "edge_discovery_maximum_quote_age_seconds": _number(
            _setting(settings, "CRYPTO_EDGE_DISCOVERY_MAX_QUOTE_AGE_SECONDS", 2.0), 2.0
        ),
        "edge_discovery_minimum_aligned_sources": max(
            0,
            _integer(_setting(settings, "CRYPTO_EDGE_DISCOVERY_MIN_ALIGNED_SOURCES", 1), 1),
        ),
        "edge_discovery_maximum_opposing_sources": max(
            0,
            _integer(_setting(settings, "CRYPTO_EDGE_DISCOVERY_MAX_OPPOSING_SOURCES", 1), 1),
        ),
        "edge_discovery_flow_source_strength": max(
            0.0,
            _number(_setting(settings, "CRYPTO_EDGE_DISCOVERY_FLOW_SOURCE_STRENGTH", 0.05), 0.05),
        ),
        "edge_discovery_uncertainty_floor_pp": max(
            0.0,
            _number(_setting(settings, "CRYPTO_EDGE_DISCOVERY_UNCERTAINTY_FLOOR_PP", 2.5), 2.5),
        ),
        "edge_discovery_maker_cancel_minutes": max(
            0.0,
            _number(_setting(settings, "CRYPTO_EDGE_DISCOVERY_MAKER_CANCEL_MINUTES", 1.5), 1.5),
        ),
        "settlement_grace_minutes": max(
            0.0,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_SETTLEMENT_GRACE_MINUTES", 2.0), 2.0),
        ),
        "settlement_retry_minutes": max(
            0.01,
            _number(_setting(settings, "CRYPTO_CYCLE_SHADOW_SETTLEMENT_RETRY_MINUTES", 5.0), 5.0),
        ),
        "max_settlement_checks_per_scan": max(
            1,
            _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_MAX_SETTLEMENT_CHECKS_PER_SCAN", 20), 20),
        ),
        "max_records": max(100, _integer(_setting(settings, "CRYPTO_CYCLE_SHADOW_MAX_RECORDS", 5000), 5000)),
    }
    identity = json.dumps(config, sort_keys=True, separators=(",", ":"))
    config["config_hash"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return config


def _new_bot(number, config):
    return {
        "bot_number": number,
        "status": "active",
        "cycle_number": 1,
        "cycles_resolved": 0,
        "cycles_won": 0,
        "cycles_lost": 0,
        "attempts": 0,
        "cycle_profit": 0.0,
        "total_profit": 0.0,
        "open_position_id": None,
        "starting_bankroll": config["starting_bankroll_per_bot"],
    }


def empty_state(settings=None, now=None):
    config = configuration(settings)
    timestamp = _iso(now)
    return {
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "created_at": timestamp,
        "updated_at": timestamp,
        "configuration": config,
        "bots": [_new_bot(index + 1, config) for index in range(config["bot_count"])],
        "records": [],
        "edge_discovery_records": [],
        "cycles": [],
        "last_funnel": {},
        "last_edge_discovery_funnel": {},
    }


def normalize_state(state, settings=None, now=None):
    config = configuration(settings)
    if not isinstance(state, dict) or state.get("version") not in COMPATIBLE_VERSIONS:
        return empty_state(settings, now=now)
    normalized = dict(state)
    normalized.update({
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "configuration": config,
        "updated_at": _iso(now),
    })
    normalized.setdefault("created_at", _iso(now))
    normalized["records"] = list(normalized.get("records") or [])
    normalized["edge_discovery_records"] = list(
        normalized.get("edge_discovery_records") or []
    )
    normalized["cycles"] = list(normalized.get("cycles") or [])
    normalized["last_funnel"] = dict(normalized.get("last_funnel") or {})
    normalized["last_edge_discovery_funnel"] = dict(
        normalized.get("last_edge_discovery_funnel") or {}
    )
    existing = {
        _integer(row.get("bot_number")): dict(row)
        for row in (normalized.get("bots") or [])
        if isinstance(row, dict)
    }
    bots = []
    for number in range(1, config["bot_count"] + 1):
        bot = _new_bot(number, config)
        bot.update(existing.get(number) or {})
        bot["bot_number"] = number
        bot["starting_bankroll"] = config["starting_bankroll_per_bot"]
        bot["attempts"] = max(0, _integer(bot.get("attempts")))
        bot["cycle_profit"] = _rounded(bot.get("cycle_profit"))
        bot["total_profit"] = _rounded(bot.get("total_profit"))
        bots.append(bot)
    normalized["bots"] = bots
    return normalized


def _selected_probability(candidate):
    side = str(candidate.get("side") or "yes").strip().lower()
    direct = candidate.get("selected_side_probability")
    if direct is not None:
        return _number(direct)
    p_yes = (candidate.get("probability") or {}).get("p_yes")
    p_yes = _number(p_yes, 50.0)
    return p_yes if side == "yes" else 100.0 - p_yes


def _selected_market_midpoint(candidate, entry):
    side = str(candidate.get("side") or "yes").strip().lower()
    probability = candidate.get("probability") or {}
    market_yes = probability.get("market_implied_yes")
    if market_yes is not None:
        market_yes = _number(market_yes)
        if 0.0 <= market_yes <= 1.0:
            market_yes *= 100.0
        return market_yes if side == "yes" else 100.0 - market_yes
    book = candidate.get("kalshi_microstructure") or {}
    selected_bid = book.get("best_yes_bid_cents" if side == "yes" else "best_no_bid_cents")
    if selected_bid is not None:
        return (_number(selected_bid) + entry) / 2.0
    return entry


def _selected_target_sigma(candidate):
    side = str(candidate.get("side") or "yes").strip().lower()
    raw = _number((candidate.get("probability") or {}).get("target_distance_sigma"), 0.0)
    return raw if side == "yes" else -raw


def _flow_counts(candidate, minimum_strength):
    side = str(candidate.get("side") or "yes").strip().lower()
    sign = 1.0 if side == "yes" else -1.0
    source_scores = (candidate.get("flow_review") or {}).get("source_scores") or {}
    aligned = []
    opposing = []
    for source, raw in source_scores.items():
        score = _number(raw)
        selected_score = sign * score
        if selected_score >= minimum_strength:
            aligned.append(str(source))
        elif selected_score <= -minimum_strength:
            opposing.append(str(source))
    return aligned, opposing


def _spread(candidate, side):
    book = candidate.get("kalshi_microstructure") or {}
    value = book.get(f"spread_{side}_cents")
    if value is None:
        value = book.get("spread_cents")
    if value is None:
        # A binary YES/NO book has the same complemented spread on both sides;
        # the microstructure adapter currently publishes the YES name only.
        value = book.get("spread_yes_cents")
    return _number(value, 999.0)


def _expiry_key(candidate):
    raw = str(candidate.get("close_time") or candidate.get("expiration_time") or "").strip()
    if not raw:
        return ""
    try:
        return _iso(raw)
    except (TypeError, ValueError):
        return raw


def candidate_review(candidate, config):
    candidate = candidate or {}
    side = str(candidate.get("side") or "yes").strip().lower()
    asset = str(candidate.get("asset") or "").strip().upper()
    ticker = str(candidate.get("ticker") or "").strip()
    entry = _number(candidate.get("entry_price"), -1.0)
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    quality_raw = candidate.get("data_quality")
    quality = _number(quality_raw.get("score") if isinstance(quality_raw, dict) else quality_raw)
    book = candidate.get("kalshi_microstructure") or {}
    execution = candidate.get("execution_tier_pricing") or {}
    age = _number(book.get("age_seconds"), 999.0)
    spread = _spread(candidate, side)
    slippage = _number(
        candidate.get("expected_slippage_cents"),
        execution.get("expected_adverse_selection_cents", 0.0),
    )
    model_probability = _selected_probability(candidate)
    market_midpoint = _selected_market_midpoint(candidate, entry)
    advantage = model_probability - market_midpoint
    target_sigma = _selected_target_sigma(candidate)
    aligned, opposing = _flow_counts(candidate, config["minimum_flow_source_strength"])
    settlement_source = str((candidate.get("probability") or {}).get("settlement_source") or "").strip()
    exact_fee = bool(candidate.get("fee_schedule_exact"))
    sequence_valid = bool(book.get("sequence_valid"))
    execution_available = bool(execution.get("available"))
    book_consistent = bool(execution.get("book_consistent"))
    available_contracts = _number(execution.get("total_available_contracts"), 0.0)

    common = []
    if not config["enabled"]:
        common.append("shadow_disabled")
    if not ticker:
        common.append("ticker_unavailable")
    if not _expiry_key(candidate):
        common.append("expiry_unavailable")
    if asset not in config["allowed_assets"]:
        common.append("asset_not_allowed")
    if asset == "XRP":
        common.append("xrp_disabled")
    if side not in {"yes", "no"}:
        common.append("side_invalid")
    if not (0 < entry < 100):
        common.append("entry_price_invalid")
    if not settlement_source:
        common.append("settlement_mapping_unavailable")
    if not exact_fee:
        common.append("exact_fee_unavailable")
    if not sequence_valid:
        common.append("orderbook_sequence_invalid")
    if not execution_available:
        common.append("executable_depth_unavailable")
    if not book_consistent:
        common.append("orderbook_inconsistent")
    if available_contracts < 1:
        common.append("executable_depth_insufficient")

    early = list(common)
    if not (config["early_minimum_minutes"] <= minutes <= config["early_maximum_minutes"]):
        early.append("early_time_window")
    if not (config["early_minimum_price_cents"] <= entry <= config["early_maximum_price_cents"]):
        early.append("early_price_window")
    if advantage < config["early_minimum_model_advantage_pp"]:
        early.append("early_model_advantage")
    if len(opposing) < config["early_minimum_opposing_sources"]:
        early.append("early_flow_opposition")
    if quality < config["early_minimum_data_quality"]:
        early.append("early_data_quality")
    if spread > config["early_maximum_spread_cents"]:
        early.append("early_spread")
    if age > config["early_maximum_quote_age_seconds"]:
        early.append("early_quote_age")

    late = list(common)
    if not (config["late_minimum_minutes"] <= minutes <= config["late_maximum_minutes"]):
        late.append("late_time_window")
    if not (config["late_minimum_price_cents"] <= entry <= config["late_maximum_price_cents"]):
        late.append("late_price_window")
    if quality < config["late_minimum_data_quality"]:
        late.append("late_data_quality")
    if spread > config["late_maximum_spread_cents"]:
        late.append("late_spread")
    if age > config["late_maximum_quote_age_seconds"]:
        late.append("late_quote_age")
    if slippage > config["late_maximum_expected_slippage_cents"]:
        late.append("late_slippage")

    lane = "early_flow_fade" if not early else ("late_convex" if not late else None)
    reasons = [] if lane else sorted(set(early + late))
    if lane == "early_flow_fade":
        rank = (2.0, advantage, target_sigma, model_probability / max(entry, 1.0))
    elif lane == "late_convex":
        rank = (1.0, target_sigma, model_probability / max(entry, 1.0), advantage)
    else:
        rank = (0.0, 0.0, 0.0, 0.0)
    return {
        "eligible": lane is not None,
        "lane": lane,
        "reasons": reasons,
        "early_reasons": early,
        "late_reasons": late,
        "rank": rank,
        "observed": {
            "asset": asset,
            "ticker": ticker,
            "side": side,
            "entry_price_cents": _rounded(entry),
            "minutes_remaining": _rounded(minutes),
            "data_quality": _rounded(quality),
            "spread_cents": _rounded(spread),
            "quote_age_seconds": _rounded(age),
            "expected_slippage_cents": _rounded(slippage),
            "selected_model_probability": _rounded(model_probability),
            "selected_market_midpoint": _rounded(market_midpoint),
            "model_advantage_pp": _rounded(advantage),
            "selected_target_distance_sigma": _rounded(target_sigma),
            "aligned_sources": aligned,
            "opposing_sources": opposing,
            "available_contracts": _rounded(available_contracts),
        },
    }


def _maker_quote(candidate, review, config, contracts, captured_at):
    if not config["maker_counterfactual_enabled"] or review["lane"] != "early_flow_fade":
        return {"status": "not_applicable", "reason": "lane_or_feature_disabled"}
    observed = review["observed"]
    if observed["spread_cents"] < config["maker_minimum_spread_cents"]:
        return {"status": "not_offered", "reason": "spread_below_maker_minimum"}
    side = observed["side"]
    book = candidate.get("kalshi_microstructure") or {}
    bid = book.get("best_yes_bid_cents" if side == "yes" else "best_no_bid_cents")
    ask = observed["entry_price_cents"]
    if bid is None:
        bid = ask - observed["spread_cents"]
    maker_price = min(ask - 1.0, _number(bid) + 1.0)
    if maker_price <= 0 or maker_price >= ask:
        return {"status": "not_offered", "reason": "no_inside_spread_price"}
    fee = kalshi_order_fee(
        maker_price,
        contracts,
        schedule=candidate.get("fee_schedule") or {},
        liquidity_role="maker",
    )
    return {
        "status": "pending",
        "price_cents": _rounded(maker_price),
        "contracts": contracts,
        "fee_dollars": _rounded(fee.get("fee_dollars")),
        "fee_exact": bool(fee.get("exact")),
        "proposed_at": captured_at,
        "last_observed_trade_ts": (candidate.get("kalshi_microstructure") or {}).get("last_trade_ts"),
        "cancel_at_minutes_remaining": config["maker_cancel_minutes"],
        "queue_position_modeled": False,
        "affects_execution": False,
    }


def _executable_cost(candidate, observed, contracts):
    """Return a complete taker walk for an integer hypothetical order."""
    contracts = max(0, int(contracts))
    if contracts <= 0:
        return None
    side = observed["side"]
    book = candidate.get("kalshi_microstructure") or {}
    contra_levels = book.get("no_levels" if side == "yes" else "yes_levels") or []
    levels = []
    for raw in contra_levels:
        try:
            contra_price = float(raw[0])
            available = float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 < contra_price < 100 and available > 0:
            levels.append((100.0 - contra_price, available))
    levels.sort(key=lambda item: item[0])
    # Tests and archived candidates may contain the already-verified aggregate
    # without its raw ladder. Live candidates normally take the ladder path.
    if not levels and observed["available_contracts"] >= contracts:
        levels = [(observed["entry_price_cents"], float(contracts))]
    remaining = float(contracts)
    book_cost = 0.0
    total_fee = 0.0
    fills = []
    for price, available in levels:
        if remaining <= 1e-9:
            break
        fill_count = min(remaining, available)
        fee = kalshi_order_fee(
            price,
            fill_count,
            schedule=candidate.get("fee_schedule") or {},
            liquidity_role="taker",
        )
        if not fee.get("exact"):
            return None
        fill_cost = fill_count * price / 100.0
        book_cost += fill_cost
        total_fee += _number(fee.get("fee_dollars"))
        fills.append({
            "price_cents": _rounded(price),
            "contracts": _rounded(fill_count),
            "fee_dollars": _rounded(fee.get("fee_dollars")),
        })
        remaining -= fill_count
    if remaining > 1e-9:
        return None
    adverse = contracts * max(0.0, observed["expected_slippage_cents"]) / 100.0
    total_cost = book_cost + total_fee + adverse
    return {
        "contracts": contracts,
        "fills": fills,
        "book_cost_dollars": book_cost,
        "fee_dollars": total_fee,
        "expected_adverse_selection_dollars": adverse,
        "total_cost_dollars": total_cost,
        "vwap_entry_price_cents": 100.0 * book_cost / contracts,
        "marginal_entry_price_cents": fills[-1]["price_cents"],
    }


def _selected_yes_probability(value, side):
    if value is None:
        return None
    probability = _number(value)
    return probability if str(side).lower() == "yes" else 100.0 - probability


def synchronized_learned_probability(candidate, maximum_residual_pp=5.0):
    """Re-anchor a market-residual prediction to the candidate's current book.

    Live reranking can refresh the Kalshi midpoint after the learned prediction
    was created. Comparing that stale probability directly with the new book
    manufactures large false edges. Preserve only the learned residual versus
    its original market anchor, then apply it to the current midpoint.
    """
    candidate = candidate or {}
    probability = candidate.get("probability") or {}
    learned = candidate.get("learned_15m_model") or {}
    raw_yes = learned.get("prob_yes", probability.get("learned_pre_anchor_prob"))
    current_market_yes = probability.get("market_implied_yes")
    reference_market_yes = learned.get("prediction_market_probability_yes")
    if raw_yes is None or current_market_yes is None:
        return {
            "available": False,
            "status": "model_or_current_market_unavailable",
        }
    raw_yes = _number(raw_yes)
    current_market_yes = _number(current_market_yes)
    if reference_market_yes is None:
        refreshed = candidate.get("live_underlying_refresh") or {}
        if refreshed.get("enabled"):
            return {
                "available": False,
                "status": "prediction_market_anchor_missing_after_refresh",
            }
        reference_market_yes = current_market_yes
        status = "scan_snapshot_assumed_synchronized"
    else:
        reference_market_yes = _number(reference_market_yes)
        status = (
            "scan_snapshot_synchronized"
            if abs(current_market_yes - reference_market_yes) <= 0.01
            else "market_residual_reanchored_after_live_refresh"
        )
    residual_pp = raw_yes - reference_market_yes
    residual_limit = max(0.0, _number(maximum_residual_pp, 5.0))
    applied_residual_pp = max(
        -residual_limit,
        min(residual_limit, residual_pp),
    )
    synchronized_yes = max(
        0.01,
        min(99.99, current_market_yes + applied_residual_pp),
    )
    return {
        "available": True,
        "status": status,
        "original_probability_yes": _rounded(raw_yes),
        "reference_market_probability_yes": _rounded(reference_market_yes),
        "current_market_probability_yes": _rounded(current_market_yes),
        "market_move_pp": _rounded(current_market_yes - reference_market_yes),
        "learned_residual_pp": _rounded(residual_pp),
        "maximum_residual_correction_pp": _rounded(residual_limit),
        "applied_residual_pp": _rounded(applied_residual_pp),
        "residual_clipped": abs(residual_pp - applied_residual_pp) > 1e-9,
        "probability_yes": _rounded(synchronized_yes),
    }


def edge_discovery_review(candidate, config):
    """Review an independent-model positive edge without affecting execution."""
    candidate = candidate or {}
    base_review = candidate_review(candidate, config)
    observed = dict(base_review["observed"])
    side = observed["side"]
    probability = candidate.get("probability") or {}
    learned = candidate.get("learned_15m_model") or {}
    synchronized = synchronized_learned_probability(
        candidate,
        config["edge_discovery_maximum_residual_correction_pp"],
    )
    raw_probability = _selected_yes_probability(
        synchronized.get("probability_yes") if synchronized.get("available") else None,
        side,
    )
    heuristic_probability = _selected_yes_probability(
        probability.get("heuristic_prob_before_ml", probability.get("pre_anchor_prob")),
        side,
    )
    calibrated_probability = observed["selected_model_probability"]
    market_probability = observed["selected_market_midpoint"]
    execution = _executable_cost(candidate, observed, 1)
    break_even = 100.0 * _number((execution or {}).get("total_cost_dollars"), 999.0)
    raw_edge = _number(raw_probability, -999.0) - break_even
    market_anchored_edge = calibrated_probability - break_even
    model_gap = abs(_number(raw_probability, -999.0) - market_probability)
    learned_reserve = _number(learned.get("calibration_error_reserve_pp"), 0.0)
    model_disagreement = (
        abs(_number(raw_probability) - _number(heuristic_probability)) / 2.0
        if raw_probability is not None and heuristic_probability is not None
        else 0.0
    )
    uncertainty_reserve = max(
        config["edge_discovery_uncertainty_floor_pp"],
        learned_reserve,
        model_disagreement,
    )
    raw_edge_low = raw_edge - uncertainty_reserve
    aligned, opposing = _flow_counts(
        candidate,
        config["edge_discovery_flow_source_strength"],
    )
    reasons = []
    if not config["edge_discovery_enabled"]:
        reasons.append("edge_discovery_disabled")
    if observed["asset"] not in config["allowed_assets"] or observed["asset"] == "XRP":
        reasons.append("asset_not_allowed")
    if not observed["ticker"]:
        reasons.append("ticker_unavailable")
    if not _expiry_key(candidate):
        reasons.append("expiry_unavailable")
    if not (
        config["edge_discovery_minimum_minutes"]
        <= observed["minutes_remaining"]
        <= config["edge_discovery_maximum_minutes"]
    ):
        reasons.append("discovery_time_window")
    if not (
        config["edge_discovery_minimum_price_cents"]
        <= observed["entry_price_cents"]
        <= config["edge_discovery_maximum_price_cents"]
    ):
        reasons.append("discovery_price_window")
    if observed["data_quality"] < config["edge_discovery_minimum_data_quality"]:
        reasons.append("discovery_data_quality")
    if observed["spread_cents"] > config["edge_discovery_maximum_spread_cents"]:
        reasons.append("discovery_spread")
    if observed["quote_age_seconds"] > config["edge_discovery_maximum_quote_age_seconds"]:
        reasons.append("discovery_quote_age")
    if not candidate.get("fee_schedule_exact") or not (candidate.get("fee_schedule") or {}).get("authoritative"):
        reasons.append("exact_fee_unavailable")
    book = candidate.get("kalshi_microstructure") or {}
    tier = candidate.get("execution_tier_pricing") or {}
    if not book.get("sequence_valid"):
        reasons.append("orderbook_sequence_invalid")
    if not tier.get("available") or not tier.get("book_consistent") or execution is None:
        reasons.append("executable_depth_unavailable")
    if not str(probability.get("settlement_source") or "").strip():
        reasons.append("settlement_mapping_unavailable")
    if raw_probability is None or heuristic_probability is None:
        reasons.append("independent_model_unavailable")
    if not synchronized.get("available"):
        reasons.append("independent_model_synchronization_unavailable")
    if raw_edge < config["edge_discovery_minimum_raw_edge_cents"]:
        reasons.append("raw_edge_below_minimum")
    if raw_edge_low < config["edge_discovery_minimum_raw_edge_low_cents"]:
        reasons.append("raw_edge_stress_floor")
    if model_gap > config["edge_discovery_maximum_raw_market_gap_pp"]:
        reasons.append("raw_market_disagreement_limit")
    if observed["selected_target_distance_sigma"] < config["edge_discovery_minimum_target_sigma"]:
        reasons.append("target_distance_support")
    if len(aligned) < config["edge_discovery_minimum_aligned_sources"]:
        reasons.append("directional_alignment")
    if len(opposing) > config["edge_discovery_maximum_opposing_sources"]:
        reasons.append("directional_opposition")
    decomposition = {
        "raw_model_probability": _rounded(raw_probability),
        "original_raw_model_probability_yes": synchronized.get(
            "original_probability_yes"
        ),
        "prediction_reference_market_probability_yes": synchronized.get(
            "reference_market_probability_yes"
        ),
        "current_market_probability_yes": synchronized.get(
            "current_market_probability_yes"
        ),
        "prediction_market_move_pp": synchronized.get("market_move_pp"),
        "learned_market_residual_pp": synchronized.get("learned_residual_pp"),
        "applied_market_residual_pp": synchronized.get("applied_residual_pp"),
        "maximum_residual_correction_pp": synchronized.get(
            "maximum_residual_correction_pp"
        ),
        "market_residual_clipped": synchronized.get("residual_clipped"),
        "prediction_synchronization_status": synchronized.get("status"),
        "heuristic_probability": _rounded(heuristic_probability),
        "market_midpoint_probability": _rounded(market_probability),
        "market_anchored_probability": _rounded(calibrated_probability),
        "raw_model_edge_cents": _rounded(raw_edge),
        "raw_model_edge_low_cents": _rounded(raw_edge_low),
        "raw_model_uncertainty_reserve_pp": _rounded(uncertainty_reserve),
        "market_anchored_edge_cents": _rounded(market_anchored_edge),
        "market_guardrail_adjustment_pp": _rounded(calibrated_probability - _number(raw_probability)),
        "ask_spread_drag_cents": _rounded(observed["entry_price_cents"] - market_probability),
        "book_walk_drag_cents": _rounded(
            _number((execution or {}).get("vwap_entry_price_cents"), observed["entry_price_cents"])
            - observed["entry_price_cents"]
        ),
        "exact_fee_drag_cents": _rounded(100.0 * _number((execution or {}).get("fee_dollars"))),
        "expected_slippage_drag_cents": _rounded(
            100.0 * _number((execution or {}).get("expected_adverse_selection_dollars"))
        ),
        "executable_break_even_probability": _rounded(break_even),
        "raw_market_gap_pp": _rounded(model_gap),
        "target_distance_sigma": observed["selected_target_distance_sigma"],
        "aligned_sources": aligned,
        "opposing_sources": opposing,
    }
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "observed": observed,
        "execution": execution,
        "decomposition": decomposition,
        "rank": (raw_edge_low, raw_edge, observed["data_quality"]),
    }


def _edge_discovery_maker_quote(candidate, review, config, captured_at):
    observed = review["observed"]
    side = observed["side"]
    book = candidate.get("kalshi_microstructure") or {}
    bid = book.get("best_yes_bid_cents" if side == "yes" else "best_no_bid_cents")
    ask = observed["entry_price_cents"]
    if bid is None:
        bid = ask - observed["spread_cents"]
    bid = _number(bid)
    maker_price = min(ask - 1.0, bid + 1.0) if observed["spread_cents"] >= 2.0 else bid
    if maker_price <= 0 or maker_price >= ask:
        return {"status": "not_offered", "reason": "post_only_price_unavailable"}
    fee = kalshi_order_fee(
        maker_price,
        1,
        schedule=candidate.get("fee_schedule") or {},
        liquidity_role="maker",
    )
    return {
        "status": "pending",
        "price_cents": _rounded(maker_price),
        "contracts": 1,
        "fee_dollars": _rounded(fee.get("fee_dollars")),
        "fee_exact": bool(fee.get("exact")),
        "proposed_at": captured_at,
        "last_observed_trade_ts": book.get("last_trade_ts"),
        "cancel_at_minutes_remaining": config["edge_discovery_maker_cancel_minutes"],
        "quote_style": "one_tick_inside" if observed["spread_cents"] >= 2.0 else "resting_best_bid",
        "queue_position_modeled": False,
        "affects_execution": False,
    }


def _capture_edge_discovery(state, candidates, config, now):
    existing_tickers = {
        str(row.get("ticker") or "") for row in state.get("edge_discovery_records") or []
    }
    existing_expiries = {
        str(row.get("expiry_key") or row.get("close_time") or "")
        for row in state.get("edge_discovery_records") or []
    }
    reason_counts = Counter()
    eligible = []
    reviewed = []
    for candidate in candidates:
        review = edge_discovery_review(candidate, config)
        decomposition = review["decomposition"]
        observed = review["observed"]
        reviewed.append({
            "asset": observed["asset"],
            "ticker": observed["ticker"],
            "side": observed["side"],
            "entry_price_cents": observed["entry_price_cents"],
            "minutes_remaining": observed["minutes_remaining"],
            "data_quality": observed["data_quality"],
            "synchronized_probability": decomposition.get(
                "raw_model_probability"
            ),
            "market_probability": decomposition.get(
                "market_midpoint_probability"
            ),
            "break_even_probability": decomposition.get(
                "executable_break_even_probability"
            ),
            "synchronized_edge_cents": decomposition.get(
                "raw_model_edge_cents"
            ),
            "stress_edge_cents": decomposition.get(
                "raw_model_edge_low_cents"
            ),
            "model_market_gap_pp": decomposition.get("raw_market_gap_pp"),
            "target_distance_sigma": decomposition.get(
                "target_distance_sigma"
            ),
            "synchronization_status": decomposition.get(
                "prediction_synchronization_status"
            ),
            "eligible": review["eligible"],
            "reasons": list(review["reasons"]),
        })
        for reason in review["reasons"]:
            reason_counts[reason] += 1
        ticker = str(candidate.get("ticker") or "")
        expiry = _expiry_key(candidate)
        if not review["eligible"]:
            continue
        if ticker in existing_tickers:
            reason_counts["discovery_ticker_already_observed"] += 1
            continue
        if expiry in existing_expiries:
            reason_counts["discovery_expiry_already_observed"] += 1
            continue
        eligible.append((review["rank"], candidate, review))
    # Candidates in a scan normally share an expiry. Select the strongest
    # conservative raw edge so correlated assets cannot inflate the sample.
    best_by_expiry = {}
    for rank, candidate, review in eligible:
        expiry = _expiry_key(candidate)
        if expiry not in best_by_expiry or rank > best_by_expiry[expiry][0]:
            best_by_expiry[expiry] = (rank, candidate, review)
    added = []
    for _rank, candidate, review in best_by_expiry.values():
        observed = review["observed"]
        execution = review["execution"]
        captured_at = _iso(now)
        row = {
            "id": f"edge-discovery-{observed['ticker']}",
            "version": VERSION,
            "mode": MODE,
            "strategy": "positive_edge_discovery_v3_bounded_residual",
            "affects_execution": False,
            "automatic_promotion": False,
            "status": "open",
            "captured_at": captured_at,
            "asset": observed["asset"],
            "ticker": observed["ticker"],
            "event_ticker": candidate.get("event_ticker"),
            "series_ticker": candidate.get("series_ticker"),
            "side": observed["side"],
            "close_time": _expiry_key(candidate),
            "expiry_key": _expiry_key(candidate),
            "minutes_remaining": observed["minutes_remaining"],
            "entry_price_cents": observed["entry_price_cents"],
            "vwap_entry_price_cents": _rounded(execution["vwap_entry_price_cents"]),
            "total_cost_dollars": _rounded(execution["total_cost_dollars"]),
            "fee_dollars": _rounded(execution["fee_dollars"]),
            "expected_adverse_selection_dollars": _rounded(
                execution["expected_adverse_selection_dollars"]
            ),
            "fills": execution["fills"],
            "data_quality": observed["data_quality"],
            "spread_cents": observed["spread_cents"],
            "quote_age_seconds": observed["quote_age_seconds"],
            "edge_decomposition": review["decomposition"],
            "maker_counterfactual": _edge_discovery_maker_quote(
                candidate, review, config, captured_at
            ),
            "settlement_checks": 0,
            "next_settlement_check_at": None,
        }
        state["edge_discovery_records"].append(row)
        existing_tickers.add(row["ticker"])
        existing_expiries.add(row["expiry_key"])
        added.append(row)
    funnel = {
        "scanned": len(candidates),
        "qualified_snapshots": len(eligible),
        "captured": len(added),
        "reason_counts": dict(reason_counts.most_common()),
        "selection_policy": "highest_raw_edge_low_per_independent_expiry",
        "best_reviewed": sorted(
            reviewed,
            key=lambda row: (
                bool(row.get("eligible")),
                -len(row.get("reasons") or []),
                _number(row.get("stress_edge_cents"), -999.0),
                _number(row.get("synchronized_edge_cents"), -999.0),
            ),
            reverse=True,
        )[:5],
    }
    return added, funnel


def _position_plan(bot, candidate, review, config, now):
    observed = review["observed"]
    entry = observed["entry_price_cents"]
    slippage_per_contract = max(0.0, observed["expected_slippage_cents"] / 100.0)
    deficit = max(0.0, -_number(bot.get("cycle_profit")))
    target_profit = deficit + config["cycle_goal_dollars"]
    contracts = None
    execution = None
    for count in range(1, config["max_contracts"] + 1):
        candidate_execution = _executable_cost(candidate, observed, count)
        if candidate_execution is None:
            return None, "executable_depth_for_plan"
        if count - candidate_execution["total_cost_dollars"] >= target_profit - 1e-9:
            contracts = count
            execution = candidate_execution
            break
    if contracts is None:
        return None, "recovery_contract_cap"
    total_cost = execution["total_cost_dollars"]
    available = int(math.floor(observed["available_contracts"] + 1e-9))
    if contracts > available:
        return None, "executable_depth_for_plan"
    if total_cost > config["max_attempt_cost_dollars"] + 1e-9:
        return None, "attempt_cost_cap"
    if deficit + total_cost > config["max_cycle_loss_dollars"] + 1e-9:
        return None, "cycle_loss_cap"
    timestamp = _iso(now)
    flat_execution = _executable_cost(candidate, observed, 1)
    if flat_execution is None:
        return None, "executable_depth_for_plan"
    flat_cost = flat_execution["total_cost_dollars"]
    record_id = f"v6-{_integer(bot.get('bot_number'))}-{_integer(bot.get('cycle_number'))}-{_integer(bot.get('attempts')) + 1}-{observed['ticker']}"
    attempt = _integer(bot.get("attempts")) + 1
    row = {
        "id": record_id,
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "status": "open",
        "captured_at": timestamp,
        "bot_number": _integer(bot.get("bot_number")),
        "cycle_number": _integer(bot.get("cycle_number")),
        "attempt": attempt,
        "attempt_type": "base" if attempt == 1 else "bounded_recovery",
        "lane": review["lane"],
        "asset": observed["asset"],
        "ticker": observed["ticker"],
        "event_ticker": candidate.get("event_ticker"),
        "series_ticker": candidate.get("series_ticker"),
        "side": observed["side"],
        "close_time": _expiry_key(candidate),
        "expiry_key": _expiry_key(candidate),
        "minutes_remaining": observed["minutes_remaining"],
        "entry_price_cents": entry,
        "vwap_entry_price_cents": _rounded(execution["vwap_entry_price_cents"]),
        "marginal_entry_price_cents": _rounded(execution["marginal_entry_price_cents"]),
        "model_probability": observed["selected_model_probability"],
        "model_probability_low": _number(candidate.get("selected_side_probability_low")),
        "model_probability_high": _number(candidate.get("selected_side_probability_high")),
        "market_midpoint": observed["selected_market_midpoint"],
        "model_advantage_pp": observed["model_advantage_pp"],
        "target_distance_sigma": observed["selected_target_distance_sigma"],
        "data_quality": observed["data_quality"],
        "spread_cents": observed["spread_cents"],
        "quote_age_seconds": observed["quote_age_seconds"],
        "opposing_sources": observed["opposing_sources"],
        "aligned_sources": observed["aligned_sources"],
        "contracts": contracts,
        "fills": execution["fills"],
        "stake_dollars": _rounded(execution["book_cost_dollars"]),
        "fee_dollars": _rounded(execution["fee_dollars"]),
        "expected_adverse_selection_dollars": _rounded(execution["expected_adverse_selection_dollars"]),
        "total_cost_dollars": _rounded(total_cost),
        "profit_if_win_dollars": _rounded(contracts - total_cost),
        "cycle_deficit_before": _rounded(deficit),
        "cycle_goal_dollars": config["cycle_goal_dollars"],
        "sizing_mode": "deficit_target_bounded",
        "fee_schedule_exact": True,
        "settlement_source": (candidate.get("probability") or {}).get("settlement_source"),
        "flat_counterfactual": {
            "contracts": 1,
            "total_cost_dollars": _rounded(flat_cost),
            "affects_execution": False,
        },
        "maker_counterfactual": {},
        "settlement_checks": 0,
        "next_settlement_check_at": None,
    }
    row["maker_counterfactual"] = _maker_quote(candidate, review, config, contracts, timestamp)
    return row, None


def _trade_timestamp(candidate):
    value = (candidate.get("kalshi_microstructure") or {}).get("last_trade_ts")
    try:
        return _now(value) if value else None
    except (TypeError, ValueError):
        return None


def _update_maker_counterfactuals(state, candidates, config, now):
    current = _now(now)
    candidate_map = {str(row.get("ticker") or ""): row for row in candidates if row.get("ticker")}
    all_records = [
        *(state.get("records") or []),
        *(state.get("edge_discovery_records") or []),
    ]
    for record in all_records:
        maker = record.get("maker_counterfactual") or {}
        if record.get("status") != "open" or maker.get("status") != "pending":
            continue
        candidate = candidate_map.get(str(record.get("ticker") or ""))
        if candidate:
            trade_ts = _trade_timestamp(candidate)
            proposed_at = _now(maker.get("proposed_at"))
            previous_trade = _now(maker.get("last_observed_trade_ts")) if maker.get("last_observed_trade_ts") else None
            raw_trade = (candidate.get("kalshi_microstructure") or {}).get("last_trade_price_cents")
            if trade_ts and trade_ts > proposed_at and (previous_trade is None or trade_ts > previous_trade) and raw_trade is not None:
                selected_trade = _number(raw_trade)
                if str(record.get("side")) == "no":
                    selected_trade = 100.0 - selected_trade
                maker["last_observed_trade_ts"] = _iso(trade_ts)
                if selected_trade <= _number(maker.get("price_cents")) + 1e-9:
                    maker.update({
                        "status": "filled",
                        "filled_at": _iso(trade_ts),
                        "public_trade_price_cents": _rounded(selected_trade),
                        "fill_evidence": "subsequent_public_trade_at_or_through_limit",
                    })
        close = _now(record.get("close_time"))
        minutes_remaining = (close - current).total_seconds() / 60.0
        cancel_minutes = _number(
            maker.get("cancel_at_minutes_remaining"),
            config["maker_cancel_minutes"],
        )
        if maker.get("status") == "pending" and minutes_remaining <= cancel_minutes:
            maker.update({"status": "expired", "expired_at": _iso(current), "reason": "maker_window_closed"})
        record["maker_counterfactual"] = maker


def _market_result(payload):
    payload = payload or {}
    status = str(payload.get("status") or "").strip().lower()
    result = str(payload.get("result") or payload.get("settlement_value") or "").strip().lower()
    if result in {"1", "yes", "y", "true"}:
        result = "yes"
    elif result in {"0", "no", "n", "false"}:
        result = "no"
    else:
        result = ""
    finalized = status == "finalized" and result in {"yes", "no"}
    return finalized, result, payload.get("settlement_ts") or payload.get("settled_time")


def _finish_cycle(state, bot, config, reason, now):
    profit = _rounded(bot.get("cycle_profit"))
    outcome = "win" if profit >= config["cycle_goal_dollars"] - 1e-9 else "loss"
    cycle = {
        "id": f"v6-cycle-{bot['bot_number']}-{bot['cycle_number']}",
        "bot_number": bot["bot_number"],
        "cycle_number": bot["cycle_number"],
        "outcome": outcome,
        "reason": reason,
        "attempts": _integer(bot.get("attempts")),
        "profit": profit,
        "resolved_at": _iso(now),
        "affects_execution": False,
    }
    state["cycles"].append(cycle)
    bot["cycles_resolved"] = _integer(bot.get("cycles_resolved")) + 1
    bot["cycles_won" if outcome == "win" else "cycles_lost"] = _integer(
        bot.get("cycles_won" if outcome == "win" else "cycles_lost")
    ) + 1
    bot["cycle_number"] = _integer(bot.get("cycle_number")) + 1
    bot["attempts"] = 0
    bot["cycle_profit"] = 0.0
    bot["open_position_id"] = None
    bot["status"] = "complete" if bot["cycles_resolved"] >= config["cycle_count"] else "active"
    return cycle


def _settle(state, fetch_market, config, now):
    current = _now(now)
    settled = []
    cycles = []
    checked = 0
    bots = {_integer(row.get("bot_number")): row for row in state.get("bots") or []}
    for record in state.get("records") or []:
        if record.get("status") != "open" or checked >= config["max_settlement_checks_per_scan"]:
            continue
        close = _now(record.get("close_time"))
        if current < close + timedelta(minutes=config["settlement_grace_minutes"]):
            continue
        next_check = record.get("next_settlement_check_at")
        if next_check and current < _now(next_check):
            continue
        checked += 1
        record["settlement_checks"] = _integer(record.get("settlement_checks")) + 1
        try:
            payload = fetch_market(record.get("ticker")) or {}
        except Exception as exc:  # The shadow ledger must never interrupt the live scan.
            payload = {"status": "error", "error": type(exc).__name__}
        finalized, result, settled_at = _market_result(payload)
        if not finalized:
            record["next_settlement_check_at"] = _iso(current + timedelta(minutes=config["settlement_retry_minutes"]))
            continue
        won = result == str(record.get("side") or "").lower()
        actual_profit = (_integer(record.get("contracts")) if won else 0) - _number(record.get("total_cost_dollars"))
        flat = record.get("flat_counterfactual") or {}
        flat_profit = (_integer(flat.get("contracts"), 1) if won else 0) - _number(flat.get("total_cost_dollars"))
        record.update({
            "status": "settled",
            "result": result,
            "won": won,
            "settled_at": settled_at or _iso(current),
            "virtual_profit": _rounded(actual_profit),
        })
        flat["virtual_profit"] = _rounded(flat_profit)
        flat["won"] = won
        record["flat_counterfactual"] = flat
        maker = record.get("maker_counterfactual") or {}
        if maker.get("status") == "filled":
            maker_cost = _integer(maker.get("contracts")) * _number(maker.get("price_cents")) / 100.0 + _number(maker.get("fee_dollars"))
            maker["total_cost_dollars"] = _rounded(maker_cost)
            maker["virtual_profit"] = _rounded((_integer(maker.get("contracts")) if won else 0) - maker_cost)
            maker["matched_taker_profit"] = _rounded(actual_profit)
            maker["incremental_profit_vs_taker"] = _rounded(maker["virtual_profit"] - actual_profit)
        record["maker_counterfactual"] = maker
        bot = bots.get(_integer(record.get("bot_number")))
        if bot:
            bot["open_position_id"] = None
            bot["cycle_profit"] = _rounded(_number(bot.get("cycle_profit")) + actual_profit)
            bot["total_profit"] = _rounded(_number(bot.get("total_profit")) + actual_profit)
            reason = None
            if bot["cycle_profit"] >= config["cycle_goal_dollars"] - 1e-9:
                reason = "cycle_target_reached"
            elif _integer(bot.get("attempts")) >= config["max_attempts"]:
                reason = "maximum_attempts_reached"
            elif bot["cycle_profit"] <= -config["max_cycle_loss_dollars"] + 1e-9:
                reason = "cycle_loss_cap_reached"
            if reason:
                cycles.append(_finish_cycle(state, bot, config, reason, current))
        settled.append(record)
    return settled, cycles


def _settle_edge_discovery(state, fetch_market, config, now):
    current = _now(now)
    settled = []
    checked = 0
    for record in state.get("edge_discovery_records") or []:
        if record.get("status") != "open" or checked >= config["max_settlement_checks_per_scan"]:
            continue
        close = _now(record.get("close_time"))
        if current < close + timedelta(minutes=config["settlement_grace_minutes"]):
            continue
        next_check = record.get("next_settlement_check_at")
        if next_check and current < _now(next_check):
            continue
        checked += 1
        record["settlement_checks"] = _integer(record.get("settlement_checks")) + 1
        try:
            payload = fetch_market(record.get("ticker")) or {}
        except Exception as exc:
            payload = {"status": "error", "error": type(exc).__name__}
        finalized, result, settled_at = _market_result(payload)
        if not finalized:
            record["next_settlement_check_at"] = _iso(
                current + timedelta(minutes=config["settlement_retry_minutes"])
            )
            continue
        won = result == str(record.get("side") or "").lower()
        outcome = 1.0 if won else 0.0
        taker_profit = outcome - _number(record.get("total_cost_dollars"))
        decomposition = record.get("edge_decomposition") or {}
        raw_probability = _number(decomposition.get("raw_model_probability")) / 100.0
        anchored_probability = _number(
            decomposition.get("market_anchored_probability")
        ) / 100.0
        record.update({
            "status": "settled",
            "result": result,
            "won": won,
            "settled_at": settled_at or _iso(current),
            "raw_model_taker_profit": _rounded(taker_profit),
            "market_anchored_taker_profit": _rounded(taker_profit),
            "raw_model_brier": _rounded((raw_probability - outcome) ** 2, 6),
            "market_anchored_brier": _rounded(
                (anchored_probability - outcome) ** 2, 6
            ),
        })
        maker = record.get("maker_counterfactual") or {}
        if maker.get("status") == "filled":
            maker_cost = (
                _number(maker.get("price_cents")) / 100.0
                + _number(maker.get("fee_dollars"))
            )
            maker["total_cost_dollars"] = _rounded(maker_cost)
            maker["virtual_profit"] = _rounded(outcome - maker_cost)
            maker["matched_taker_profit"] = _rounded(taker_profit)
            maker["incremental_profit_vs_taker"] = _rounded(
                maker["virtual_profit"] - taker_profit
            )
        elif maker.get("status") == "pending":
            maker.update({
                "status": "expired",
                "expired_at": _iso(current),
                "reason": "market_settled_before_public_trade_fill",
            })
        record["maker_counterfactual"] = maker
        settled.append(record)
    return settled


def _capture(state, candidates, config, now):
    added = []
    closed = []
    reason_counts = Counter()
    reviewed = []
    used_tickers = {str(row.get("ticker")) for row in state.get("records") or []}
    used_expiries = {str(row.get("expiry_key") or row.get("close_time")) for row in state.get("records") or []}
    for candidate in candidates:
        review = candidate_review(candidate, config)
        for reason in review["reasons"]:
            reason_counts[reason] += 1
        if not review["eligible"]:
            continue
        ticker = str(candidate.get("ticker") or "")
        expiry = _expiry_key(candidate)
        if ticker in used_tickers:
            reason_counts["ticker_already_observed"] += 1
            continue
        if expiry in used_expiries:
            reason_counts["expiry_already_observed"] += 1
            continue
        reviewed.append((review["rank"], candidate, review))
    reviewed.sort(key=lambda row: row[0], reverse=True)

    available_bots = [
        bot for bot in state.get("bots") or []
        if bot.get("status") == "active" and not bot.get("open_position_id")
    ]
    available_bots.sort(key=lambda bot: (
        0 if _number(bot.get("cycle_profit")) < 0 else 1,
        _number(bot.get("cycle_profit")),
        _integer(bot.get("cycles_resolved")),
        _integer(bot.get("bot_number")),
    ))

    for bot in available_bots:
        chosen = None
        for index, (_rank, candidate, review) in enumerate(reviewed):
            ticker = str(candidate.get("ticker") or "")
            expiry = _expiry_key(candidate)
            if ticker in used_tickers or expiry in used_expiries:
                continue
            plan, failure = _position_plan(bot, candidate, review, config, now)
            if plan is None and _number(bot.get("cycle_profit")) < 0 and failure in {
                "recovery_contract_cap", "attempt_cost_cap", "cycle_loss_cap"
            }:
                closed.append(_finish_cycle(state, bot, config, failure, now))
                plan, failure = _position_plan(bot, candidate, review, config, now)
            if plan is None:
                reason_counts[failure or "sizing_unavailable"] += 1
                continue
            chosen = (index, plan, ticker, expiry)
            break
        if not chosen:
            continue
        index, plan, ticker, expiry = chosen
        reviewed.pop(index)
        state["records"].append(plan)
        bot["attempts"] = _integer(bot.get("attempts")) + 1
        bot["open_position_id"] = plan["id"]
        used_tickers.add(ticker)
        used_expiries.add(expiry)
        added.append(plan)

    funnel = {
        "scanned": len(candidates),
        "eligible": sum(1 for candidate in candidates if candidate_review(candidate, config)["eligible"]),
        "captured": len(added),
        "rejected": max(0, len(candidates) - len(added)),
        "reason_counts": dict(reason_counts.most_common()),
        "assignment_policy": "recovery_deficit_first_then_lowest_completed_cycle; one_global_position_per_expiry",
        "lane_priority": "early_model_advantage_then_target_support; late_target_support_then_probability_per_cent",
    }
    return added, closed, funnel


def _performance(records, key, label):
    groups = defaultdict(list)
    for row in records:
        if row.get("status") == "settled":
            groups[str(row.get(key) or "unknown")].append(row)
    output = []
    for name, rows in sorted(groups.items()):
        cost = sum(_number(row.get("total_cost_dollars")) for row in rows)
        profit = sum(_number(row.get("virtual_profit")) for row in rows)
        wins = sum(1 for row in rows if row.get("won"))
        output.append({
            label: name,
            "settled": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "cost": _rounded(cost),
            "profit": _rounded(profit),
            "roi": _rounded(100.0 * profit / cost, 2) if cost else 0.0,
        })
    return output


def _price_band(row):
    price = _number(row.get("entry_price_cents"))
    if price < 20:
        return "10-19c"
    if price < 30:
        return "20-29c"
    if price < 35:
        return "30-34c"
    if price < 40:
        return "35-39c"
    return "40-44c"


def _edge_discovery_summary(state):
    all_records = list(state.get("edge_discovery_records") or [])
    current_strategy = "positive_edge_discovery_v3_bounded_residual"
    records = [
        row for row in all_records
        if row.get("strategy") == current_strategy
    ]
    legacy_records = [
        row for row in all_records
        if row.get("strategy") != current_strategy
    ]
    settled = [row for row in records if row.get("status") == "settled"]
    open_rows = [row for row in records if row.get("status") == "open"]
    cost = sum(_number(row.get("total_cost_dollars")) for row in settled)
    raw_profit = sum(_number(row.get("raw_model_taker_profit")) for row in settled)
    market_profit = sum(
        _number(row.get("market_anchored_taker_profit")) for row in settled
    )
    wins = sum(1 for row in settled if row.get("won"))
    maker_rows = [
        row for row in settled
        if (row.get("maker_counterfactual") or {}).get("status") == "filled"
    ]
    maker_cost = sum(
        _number((row.get("maker_counterfactual") or {}).get("total_cost_dollars"))
        for row in maker_rows
    )
    maker_profit = sum(
        _number((row.get("maker_counterfactual") or {}).get("virtual_profit"))
        for row in maker_rows
    )
    decomposition_keys = (
        "raw_model_edge_cents",
        "raw_model_edge_low_cents",
        "market_guardrail_adjustment_pp",
        "ask_spread_drag_cents",
        "book_walk_drag_cents",
        "exact_fee_drag_cents",
        "expected_slippage_drag_cents",
        "market_anchored_edge_cents",
    )
    averages = {}
    for key in decomposition_keys:
        values = [
            _number((row.get("edge_decomposition") or {}).get(key))
            for row in records
            if (row.get("edge_decomposition") or {}).get(key) is not None
        ]
        averages[key] = _rounded(sum(values) / len(values)) if values else 0.0
    raw_briers = [_number(row.get("raw_model_brier")) for row in settled]
    anchored_briers = [
        _number(row.get("market_anchored_brier")) for row in settled
    ]
    config = state.get("configuration") or {}
    return {
        "version": "positive-edge-discovery-v3",
        "strategy": current_strategy,
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "tracked": len(records),
        "legacy_diagnostic_records": len(legacy_records),
        "legacy_note": (
            "Pre-V3 records are preserved for audit but excluded; V1 could use "
            "a stale prediction/book pair and V2 did not bound extreme residuals."
        ),
        "open": len(open_rows),
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "independent_expiries": len(
            {str(row.get("expiry_key") or "") for row in settled}
        ),
        "raw_model_taker": {
            "cost": _rounded(cost),
            "profit": _rounded(raw_profit),
            "roi": _rounded(100.0 * raw_profit / cost, 2) if cost else 0.0,
            "brier": _rounded(sum(raw_briers) / len(raw_briers), 6)
            if raw_briers else None,
            "average_expected_edge_cents": averages["raw_model_edge_cents"],
            "average_edge_low_cents": averages["raw_model_edge_low_cents"],
        },
        "market_anchored_taker": {
            "cost": _rounded(cost),
            "profit": _rounded(market_profit),
            "roi": _rounded(100.0 * market_profit / cost, 2) if cost else 0.0,
            "brier": _rounded(sum(anchored_briers) / len(anchored_briers), 6)
            if anchored_briers else None,
            "average_expected_edge_cents": averages["market_anchored_edge_cents"],
        },
        "maker_post_only": {
            "offered": sum(
                1 for row in records
                if (row.get("maker_counterfactual") or {}).get("status")
                in {"pending", "filled", "expired"}
            ),
            "pending": sum(
                1 for row in records
                if (row.get("maker_counterfactual") or {}).get("status") == "pending"
            ),
            "filled": len(maker_rows),
            "expired": sum(
                1 for row in records
                if (row.get("maker_counterfactual") or {}).get("status") == "expired"
            ),
            "cost": _rounded(maker_cost),
            "profit": _rounded(maker_profit),
            "roi": _rounded(100.0 * maker_profit / maker_cost, 2)
            if maker_cost else 0.0,
            "queue_position_modeled": False,
        },
        "average_decomposition": averages,
        "requirements": {
            "minutes": [
                config.get("edge_discovery_minimum_minutes"),
                config.get("edge_discovery_maximum_minutes"),
            ],
            "price_cents": [
                config.get("edge_discovery_minimum_price_cents"),
                config.get("edge_discovery_maximum_price_cents"),
            ],
            "minimum_raw_edge_cents": config.get(
                "edge_discovery_minimum_raw_edge_cents"
            ),
            "minimum_raw_edge_low_cents": config.get(
                "edge_discovery_minimum_raw_edge_low_cents"
            ),
            "maximum_raw_market_gap_pp": config.get(
                "edge_discovery_maximum_raw_market_gap_pp"
            ),
            "maximum_residual_correction_pp": config.get(
                "edge_discovery_maximum_residual_correction_pp"
            ),
            "minimum_target_sigma": config.get(
                "edge_discovery_minimum_target_sigma"
            ),
            "minimum_data_quality": config.get(
                "edge_discovery_minimum_data_quality"
            ),
            "minimum_aligned_sources": config.get(
                "edge_discovery_minimum_aligned_sources"
            ),
            "maximum_opposing_sources": config.get(
                "edge_discovery_maximum_opposing_sources"
            ),
        },
        "evaluation": {
            "minimum_independent_expiries": config.get(
                "research_goal_markets", 100
            ),
            "ready_for_review": len(
                {str(row.get("expiry_key") or "") for row in settled}
            ) >= _integer(config.get("research_goal_markets"), 100),
            "automatic_promotion": False,
        },
        "candidate_funnel": dict(
            state.get("last_edge_discovery_funnel") or {}
        ),
        "recent_records": sorted(
            records,
            key=lambda row: str(row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }


def summarize(state):
    config = state.get("configuration") or {}
    records = list(state.get("records") or [])
    settled = [row for row in records if row.get("status") == "settled"]
    open_rows = [row for row in records if row.get("status") == "open"]
    cost = sum(_number(row.get("total_cost_dollars")) for row in settled)
    profit = sum(_number(row.get("virtual_profit")) for row in settled)
    wins = sum(1 for row in settled if row.get("won"))
    flat_cost = sum(_number((row.get("flat_counterfactual") or {}).get("total_cost_dollars")) for row in settled)
    flat_profit = sum(_number((row.get("flat_counterfactual") or {}).get("virtual_profit")) for row in settled)
    maker_rows = [row for row in settled if (row.get("maker_counterfactual") or {}).get("status") == "filled"]
    maker_cost = sum(_number((row.get("maker_counterfactual") or {}).get("total_cost_dollars")) for row in maker_rows)
    maker_profit = sum(_number((row.get("maker_counterfactual") or {}).get("virtual_profit")) for row in maker_rows)
    maker_taker_profit = sum(_number((row.get("maker_counterfactual") or {}).get("matched_taker_profit")) for row in maker_rows)
    unique_markets = len({str(row.get("ticker")) for row in settled})
    cycles = list(state.get("cycles") or [])
    cycle_wins = sum(1 for row in cycles if row.get("outcome") == "win")
    bots = []
    for row in state.get("bots") or []:
        bot = dict(row)
        bot_records = [item for item in settled if _integer(item.get("bot_number")) == _integer(row.get("bot_number"))]
        open_position = next(
            (item for item in open_rows if item.get("id") == row.get("open_position_id")),
            None,
        )
        bot["cycle_deficit"] = _rounded(max(0.0, -_number(row.get("cycle_profit"))))
        bot["loss_room_remaining"] = _rounded(max(0.0, _number(config.get("max_cycle_loss_dollars")) - bot["cycle_deficit"]))
        bot["wins"] = sum(1 for item in bot_records if item.get("won"))
        bot["losses"] = sum(1 for item in bot_records if not item.get("won"))
        bot["virtual_equity"] = _rounded(_number(row.get("starting_bankroll")) + _number(row.get("total_profit")))
        bot["open_position"] = open_position or {}
        bots.append(bot)
    price_groups = defaultdict(list)
    for row in settled:
        price_groups[_price_band(row)].append(row)
    price_performance = []
    for band, rows in sorted(price_groups.items()):
        band_cost = sum(_number(row.get("total_cost_dollars")) for row in rows)
        band_profit = sum(_number(row.get("virtual_profit")) for row in rows)
        band_wins = sum(1 for row in rows if row.get("won"))
        price_performance.append({
            "band": band,
            "settled": len(rows),
            "wins": band_wins,
            "losses": len(rows) - band_wins,
            "cost": _rounded(band_cost),
            "profit": _rounded(band_profit),
            "roi": _rounded(100.0 * band_profit / band_cost, 2) if band_cost else 0.0,
        })
    return {
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "configuration": config,
        "tracked": len(records),
        "open": len(open_rows),
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "cost": _rounded(cost),
        "profit": _rounded(profit),
        "roi": _rounded(100.0 * profit / cost, 2) if cost else 0.0,
        "cycles_resolved": len(cycles),
        "cycles_won": cycle_wins,
        "cycles_lost": len(cycles) - cycle_wins,
        "cycles_net_profitable": sum(1 for row in cycles if _number(row.get("profit")) > 0),
        "configured_cycles": _integer(config.get("cycle_count")) * _integer(config.get("bot_count")),
        "research_goal_markets": _integer(config.get("research_goal_markets"), 100),
        "bots": bots,
        "candidate_funnel": dict(state.get("last_funnel") or {}),
        "performance_by_lane": _performance(settled, "lane", "lane"),
        "performance_by_attempt": _performance(settled, "attempt", "attempt"),
        "performance_by_asset": _performance(settled, "asset", "asset"),
        "performance_by_price_band": price_performance,
        "flat_stake_counterfactual": {
            "affects_execution": False,
            "settled": len(settled),
            "cost": _rounded(flat_cost),
            "profit": _rounded(flat_profit),
            "roi": _rounded(100.0 * flat_profit / flat_cost, 2) if flat_cost else 0.0,
            "recovery_incremental_profit": _rounded(profit - flat_profit),
        },
        "maker_taker_counterfactual": {
            "affects_execution": False,
            "queue_position_modeled": False,
            "offered": sum(1 for row in records if (row.get("maker_counterfactual") or {}).get("status") in {"pending", "filled", "expired"}),
            "pending": sum(1 for row in records if (row.get("maker_counterfactual") or {}).get("status") == "pending"),
            "filled": len(maker_rows),
            "expired": sum(1 for row in records if (row.get("maker_counterfactual") or {}).get("status") == "expired"),
            "maker_cost": _rounded(maker_cost),
            "maker_profit": _rounded(maker_profit),
            "matched_taker_profit": _rounded(maker_taker_profit),
            "incremental_profit": _rounded(maker_profit - maker_taker_profit),
        },
        "positive_edge_discovery": _edge_discovery_summary(state),
        "evaluation": {
            "unique_settled_markets": unique_markets,
            "minimum_unique_markets": _integer(config.get("research_goal_markets"), 100),
            "ready_for_review": unique_markets >= _integer(config.get("research_goal_markets"), 100),
            "automatic_promotion": False,
        },
        "recent_records": sorted(records, key=lambda row: str(row.get("captured_at") or ""), reverse=True)[:100],
        "recent_cycles": sorted(cycles, key=lambda row: str(row.get("resolved_at") or ""), reverse=True)[:50],
    }


def run_scan(state, candidates, fetch_market, *, settings=None, now=None):
    current = _now(now)
    normalized = normalize_state(state, settings, now=current)
    config = normalized["configuration"]
    candidates = list(candidates or [])
    _update_maker_counterfactuals(normalized, candidates, config, current)
    settled, cycles = _settle(normalized, fetch_market, config, current)
    discovery_settled = _settle_edge_discovery(normalized, fetch_market, config, current)
    added, cap_cycles, funnel = _capture(normalized, candidates, config, current)
    discovery_added, discovery_funnel = _capture_edge_discovery(normalized, candidates, config, current)
    cycles.extend(cap_cycles)
    normalized["last_funnel"] = funnel
    normalized["last_edge_discovery_funnel"] = discovery_funnel
    if len(normalized["records"]) > config["max_records"]:
        open_rows = [row for row in normalized["records"] if row.get("status") == "open"]
        settled_rows = [row for row in normalized["records"] if row.get("status") != "open"]
        keep_settled = max(0, config["max_records"] - len(open_rows))
        normalized["records"] = settled_rows[-keep_settled:] + open_rows
    if len(normalized["edge_discovery_records"]) > config["max_records"]:
        discovery_open = [row for row in normalized["edge_discovery_records"] if row.get("status") == "open"]
        discovery_settled_rows = [row for row in normalized["edge_discovery_records"] if row.get("status") != "open"]
        keep_discovery_settled = max(0, config["max_records"] - len(discovery_open))
        normalized["edge_discovery_records"] = discovery_settled_rows[-keep_discovery_settled:] + discovery_open
    normalized["updated_at"] = _iso(current)
    summary = summarize(normalized)
    summary["positive_edge_discovery"]["captured_this_scan"] = len(discovery_added)
    summary["positive_edge_discovery"]["settled_this_scan"] = len(discovery_settled)
    return normalized, summary, added, settled, cycles
