"""Prospective production-parity shadows for crypto 15-minute markets.

Every recorded entry must pass the same kind of decision-time requirements a
live taker/FOK order would face: fresh cross-venue inputs, an authoritative fee
schedule, a fresh Kalshi book, full-size visible depth, a bounded final quote
move, and an official Kalshi settlement.  The module deliberately has no order
API, bankroll mutation, recovery logic, or automatic-promotion path.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

from crypto_pricing import kalshi_order_fee
from crypto_execution_safety import confirmation_reasons
from crypto_evidence import register_policy, retain_records
from crypto_signal_tournament_shadow import signal_reviews


VERSION = "crypto-execution-lab-shadow-v1"
MODE = "production_pipeline_shadow_only"
CHICAGO = ZoneInfo("America/Chicago")
ONE_SIDED_90_Z = NormalDist().inv_cdf(0.90)

FLOW_CORE = "flow_midprice_core"
FLOW_LATE = "flow_midprice_late"
FLOW_BTC_ETH = "flow_midprice_btc_eth"
FLOW_NO = "flow_midprice_no_diagnostic"
FLOW_PERSISTENCE = "flow_multihorizon_persistence"
SPOT_NO_CHASE = "spot_lead_no_chase"
SPOT_FLOW = "spot_flow_fresh_consensus"
SPOT_PULLBACK = "spot_lead_pullback_reentry"
SETTLEMENT_DISTANCE = "settlement_distance_favorite"

LANES = (
    FLOW_CORE,
    FLOW_LATE,
    FLOW_BTC_ETH,
    FLOW_NO,
    FLOW_PERSISTENCE,
    SPOT_NO_CHASE,
    SPOT_FLOW,
    SPOT_PULLBACK,
    SETTLEMENT_DISTANCE,
)

LANE_DESCRIPTIONS = {
    FLOW_CORE: "Coinbase and Kraken 60-second trade flow agree; 35-65c executable entry.",
    FLOW_LATE: "Core flow hypothesis restricted to 10-13 minutes before close.",
    FLOW_BTC_ETH: "Core flow hypothesis restricted to BTC and ETH.",
    FLOW_NO: "Prospective NO-side diagnostic for the observed directional asymmetry.",
    FLOW_PERSISTENCE: "Coinbase and Kraken 60s and 300s trade flow persist in one direction.",
    SPOT_NO_CHASE: "Cross-venue spot lead survives a fresh no-chase FOK quote check.",
    SPOT_FLOW: "Fresh spot lead and independent cross-venue trade flow agree.",
    SPOT_PULLBACK: "A prior spot lead is entered only after a later one-cent pullback while it persists.",
    SETTLEMENT_DISTANCE: "Late favorite backed by a conservative settlement-distance probability margin.",
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
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now(value=None):
    return _parse_time(value) or datetime.now(timezone.utc)


def _iso(value=None):
    return _now(value).isoformat()


def _sign(value, threshold=0.0):
    value = _number(value)
    if value > threshold:
        return 1
    if value < -threshold:
        return -1
    return 0


def _side(direction):
    return "yes" if direction > 0 else "no"


def _policy_hash(config):
    payload = {key: value for key, value in config.items() if key != "policy_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def configuration(settings=None):
    settings = settings or {}
    config = {
        "enabled": _boolean(
            settings.get("CRYPTO_EXECUTION_LAB_SHADOW_ENABLED", True), True
        ),
        "assets": sorted(
            {
                part.strip().upper()
                for part in str(
                    settings.get(
                        "CRYPTO_EXECUTION_LAB_SHADOW_ASSETS",
                        "BTC,ETH,SOL,DOGE,XRP",
                    )
                ).split(",")
                if part.strip()
            }
        ),
        "minimum_minutes": max(
            0.0,
            _number(settings.get("CRYPTO_EXECUTION_LAB_MIN_MINUTES", 2), 2),
        ),
        "maximum_minutes": max(
            0.0,
            _number(settings.get("CRYPTO_EXECUTION_LAB_MAX_MINUTES", 13), 13),
        ),
        "minimum_data_quality": max(
            0.0,
            min(
                1.0,
                _number(
                    settings.get("CRYPTO_EXECUTION_LAB_MIN_DATA_QUALITY", 0.80),
                    0.80,
                ),
            ),
        ),
        "maximum_quote_age_seconds": max(
            0.1,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_MAX_QUOTE_AGE_SECONDS", 2),
                2,
            ),
        ),
        "maximum_source_age_seconds": max(
            0.1,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_MAX_SOURCE_AGE_SECONDS", 4),
                4,
            ),
        ),
        "maximum_spread_cents": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_MAX_SPREAD_CENTS", 4), 4
            ),
        ),
        "maximum_adverse_move_cents": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_EXECUTION_LAB_MAX_ADVERSE_MOVE_CENTS", 1
                ),
                1,
            ),
        ),
        "flow_minimum_strength": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_FLOW_MIN_STRENGTH", 0.30),
                0.30,
            ),
        ),
        "flow_minimum_price_cents": max(
            1.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_FLOW_MIN_PRICE_CENTS", 35),
                35,
            ),
        ),
        "flow_maximum_price_cents": min(
            99.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_FLOW_MAX_PRICE_CENTS", 65),
                65,
            ),
        ),
        "flow_late_minimum_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_FLOW_LATE_MIN_MINUTES", 10),
                10,
            ),
        ),
        "spot_minimum_strength_bps": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SPOT_MIN_STRENGTH_BPS", 10),
                10,
            ),
        ),
        "spot_minimum_price_cents": max(
            1.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SPOT_MIN_PRICE_CENTS", 35),
                35,
            ),
        ),
        "spot_maximum_price_cents": min(
            99.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SPOT_MAX_PRICE_CENTS", 80),
                80,
            ),
        ),
        "spot_minimum_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SPOT_MIN_MINUTES", 5), 5
            ),
        ),
        "pullback_wait_seconds": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_PULLBACK_WAIT_SECONDS", 30),
                30,
            ),
        ),
        "pullback_expiry_seconds": max(
            15.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_PULLBACK_EXPIRY_SECONDS", 180),
                180,
            ),
        ),
        "pullback_cents": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_PULLBACK_CENTS", 1), 1
            ),
        ),
        "settlement_minimum_abs_sigma": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SETTLEMENT_MIN_ABS_SIGMA", 1.25),
                1.25,
            ),
        ),
        "settlement_minimum_conservative_edge_cents": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_EXECUTION_LAB_SETTLEMENT_MIN_EDGE_CENTS", 2
                ),
                2,
            ),
        ),
        "settlement_maximum_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SETTLEMENT_MAX_MINUTES", 6),
                6,
            ),
        ),
        "settlement_minimum_price_cents": max(
            1.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SETTLEMENT_MIN_PRICE_CENTS", 55),
                55,
            ),
        ),
        "settlement_maximum_price_cents": min(
            99.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SETTLEMENT_MAX_PRICE_CENTS", 85),
                85,
            ),
        ),
        "default_contracts": max(
            1,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_DEFAULT_CONTRACTS", 3), 3
            ),
        ),
        "confirmed_contracts": max(
            1,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_CONFIRMED_CONTRACTS", 5), 5
            ),
        ),
        "settlement_grace_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_EXECUTION_LAB_SETTLEMENT_GRACE_MINUTES", 2),
                2,
            ),
        ),
        "maximum_settlement_checks_per_scan": max(
            1,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_MAX_SETTLEMENT_CHECKS", 50),
                50,
            ),
        ),
        "minimum_review_markets": max(
            25,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_MIN_REVIEW_MARKETS", 100),
                100,
            ),
        ),
        "minimum_review_days": max(
            3,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_MIN_REVIEW_DAYS", 7), 7
            ),
        ),
        "maximum_records": max(
            1000,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_MAX_RECORDS", 10000), 10000
            ),
        ),
        "maximum_attempts": max(
            1000,
            _integer(
                settings.get("CRYPTO_EXECUTION_LAB_MAX_ATTEMPTS", 10000), 10000
            ),
        ),
        "historical_backfill": False,
        "execution_revision": "crypto-audit-2026-09-v1",
        "affects_execution": False,
        "automatic_promotion": False,
        "fill_model": "fresh_visible_taker_fok_depth",
        "lanes": list(LANES),
    }
    config["policy_hash"] = _policy_hash(config)
    return config


def empty_ledger(settings=None, now=None):
    current = _iso(now)
    return {
        "version": VERSION,
        "mode": MODE,
        "registered_at": current,
        "updated_at": current,
        "configuration": configuration(settings),
        "records": [],
        "attempts": [],
        "pending_pullbacks": [],
        "last_capture_funnel": {},
        "last_capture_ids": [],
        "last_settled_ids": [],
    }


def normalize_ledger(value, settings=None, now=None):
    ledger = value if isinstance(value, dict) else {}
    ledger["version"] = VERSION
    ledger["mode"] = MODE
    ledger.setdefault("registered_at", _iso(now))
    ledger["updated_at"] = ledger.get("updated_at") or ledger["registered_at"]
    register_policy(ledger, configuration(settings), _iso(now))
    for key in ("records", "attempts", "pending_pullbacks"):
        ledger[key] = ledger.get(key) if isinstance(ledger.get(key), list) else []
    active_policy = ledger["configuration"]["policy_hash"]
    ledger["pending_pullbacks"] = [
        row
        for row in ledger["pending_pullbacks"]
        if isinstance(row, dict) and row.get("policy_hash") == active_policy
    ]
    ledger.setdefault("last_capture_funnel", {})
    ledger.setdefault("last_capture_ids", [])
    ledger.setdefault("last_settled_ids", [])
    return ledger


def _venue(candidate, name):
    value = ((candidate.get("microstructure") or {}).get(name)) or {}
    return value if isinstance(value, dict) else {}


def _entry_price(candidate, side):
    book = candidate.get("kalshi_microstructure") or {}
    value = book.get(f"best_{side}_entry_price_cents")
    if value is None:
        value = candidate.get(f"{side}_ask")
    return _number(value, -1.0)


def _quality_review(candidate, config):
    reasons = []
    if candidate.get("market_lane") != "crypto_15m" or not candidate.get(
        "is_15m_market"
    ):
        reasons.append("unsupported_market_lane")
    if str(candidate.get("asset") or "").upper() not in config["assets"]:
        reasons.append("unsupported_asset")
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    if not config["minimum_minutes"] <= minutes <= config["maximum_minutes"]:
        reasons.append("outside_time_window")
    if _number((candidate.get("data_quality") or {}).get("score")) < config[
        "minimum_data_quality"
    ]:
        reasons.append("data_quality_below_floor")
    book = candidate.get("kalshi_microstructure") or {}
    if not book.get("fresh"):
        reasons.append("kalshi_book_stale")
    if not book.get("sequence_valid"):
        reasons.append("kalshi_sequence_invalid")
    if not book.get("book_consistent"):
        reasons.append("kalshi_book_inconsistent")
    if _number(book.get("age_seconds"), 999) > config["maximum_quote_age_seconds"]:
        reasons.append("kalshi_quote_too_old")
    if _number(book.get("spread_yes_cents"), 999) > config["maximum_spread_cents"]:
        reasons.append("kalshi_spread_too_wide")
    for source in ("coinbase", "kraken"):
        row = _venue(candidate, source)
        if not row.get("connected"):
            reasons.append(f"{source}_unavailable")
        if _number(
            row.get("stream_age_seconds", row.get("book_age_seconds")), 999
        ) > config["maximum_source_age_seconds"]:
            reasons.append(f"{source}_stale")
    schedule = candidate.get("fee_schedule") or {}
    if not schedule.get("authoritative"):
        reasons.append("fee_schedule_not_authoritative")
    return {"ok": not reasons, "reasons": list(dict.fromkeys(reasons))}


def _same_persistent_flow(candidate, threshold=0.10):
    directions = []
    evidence = {}
    for source in ("coinbase", "kraken"):
        row = _venue(candidate, source)
        flow_60 = _number(row.get("trade_flow_60s"))
        flow_300 = _number(row.get("trade_flow_300s"))
        short_direction = _sign(flow_60, threshold)
        long_direction = _sign(flow_300, threshold)
        if not short_direction or short_direction != long_direction:
            return 0, evidence
        directions.append(short_direction)
        evidence[f"{source}_flow_60s"] = flow_60
        evidence[f"{source}_flow_300s"] = flow_300
    if len(set(directions)) != 1:
        return 0, evidence
    return directions[0], evidence


def _settlement_hypothesis(candidate, config):
    if candidate.get("market_kind") != "above":
        return None
    probability = candidate.get("probability") or {}
    sigma = _number(probability.get("target_distance_sigma"))
    if abs(sigma) < config["settlement_minimum_abs_sigma"]:
        return None
    side = _side(1 if sigma > 0 else -1)
    price = _entry_price(candidate, side)
    if not (
        config["settlement_minimum_price_cents"]
        <= price
        <= config["settlement_maximum_price_cents"]
    ):
        return None
    if _number(candidate.get("minutes_to_close"), 999) > config[
        "settlement_maximum_minutes"
    ]:
        return None
    if side == "yes":
        conservative_probability = _number(
            probability.get("p_low"), probability.get("prob")
        )
    else:
        conservative_probability = 100.0 - _number(
            probability.get("p_high"), probability.get("prob")
        )
    fee = kalshi_order_fee(
        price,
        1,
        schedule=candidate.get("fee_schedule") or {},
        liquidity_role="taker",
    )
    fee_cents = 100.0 * _number(fee.get("fee_dollars"))
    conservative_edge = conservative_probability - price - fee_cents
    if conservative_edge < config["settlement_minimum_conservative_edge_cents"]:
        return None
    return {
        "lane": SETTLEMENT_DISTANCE,
        "side": side,
        "strength": abs(sigma),
        "contracts": config["default_contracts"],
        "evidence": {
            "target_distance_sigma": sigma,
            "conservative_probability": round(conservative_probability, 4),
            "conservative_edge_cents": round(conservative_edge, 4),
        },
    }


def _hypotheses(candidate, config):
    signals = signal_reviews(candidate)
    flow = signals.get("cross_venue_flow_follow") or {}
    spot = signals.get("spot_leads_kalshi") or {}
    minutes = _number(candidate.get("minutes_to_close"), -1)
    asset = str(candidate.get("asset") or "").upper()
    hypotheses = []
    flow_side = str(flow.get("side") or "")
    flow_price = _entry_price(candidate, flow_side) if flow_side else -1.0
    flow_core = bool(
        flow
        and _number(flow.get("strength")) >= config["flow_minimum_strength"]
        and config["flow_minimum_price_cents"]
        <= flow_price
        <= config["flow_maximum_price_cents"]
    )
    if flow_core:
        base = {
            "side": flow_side,
            "strength": flow.get("strength"),
            "contracts": config["default_contracts"],
            "evidence": flow.get("evidence") or {},
        }
        hypotheses.append({"lane": FLOW_CORE, **base})
        if minutes >= config["flow_late_minimum_minutes"]:
            hypotheses.append({"lane": FLOW_LATE, **base})
        if asset in {"BTC", "ETH"}:
            hypotheses.append({"lane": FLOW_BTC_ETH, **base})
        if flow_side == "no":
            hypotheses.append({"lane": FLOW_NO, **base})

    persistent_direction, persistent_evidence = _same_persistent_flow(candidate)
    if persistent_direction:
        persistent_side = _side(persistent_direction)
        persistent_price = _entry_price(candidate, persistent_side)
        if (
            config["flow_minimum_price_cents"]
            <= persistent_price
            <= config["flow_maximum_price_cents"]
        ):
            hypotheses.append(
                {
                    "lane": FLOW_PERSISTENCE,
                    "side": persistent_side,
                    "strength": min(
                        abs(_number(value)) for value in persistent_evidence.values()
                    ),
                    "contracts": config["default_contracts"],
                    "evidence": persistent_evidence,
                }
            )

    spot_side = str(spot.get("side") or "")
    spot_price = _entry_price(candidate, spot_side) if spot_side else -1.0
    spot_core = bool(
        spot
        and _number(spot.get("strength")) >= config["spot_minimum_strength_bps"]
        and config["spot_minimum_price_cents"]
        <= spot_price
        <= config["spot_maximum_price_cents"]
        and minutes >= config["spot_minimum_minutes"]
    )
    if spot_core:
        hypotheses.append(
            {
                "lane": SPOT_NO_CHASE,
                "side": spot_side,
                "strength": spot.get("strength"),
                "contracts": config["default_contracts"],
                "evidence": spot.get("evidence") or {},
            }
        )
        if flow and str(flow.get("side") or "") == spot_side:
            hypotheses.append(
                {
                    "lane": SPOT_FLOW,
                    "side": spot_side,
                    "strength": min(
                        _number(spot.get("strength")),
                        100.0 * _number(flow.get("strength")),
                    ),
                    "contracts": config["confirmed_contracts"],
                    "evidence": {
                        "spot": spot.get("evidence") or {},
                        "flow": flow.get("evidence") or {},
                    },
                }
            )
    settlement = _settlement_hypothesis(candidate, config)
    if settlement:
        hypotheses.append(settlement)
    return hypotheses, spot if spot_core else {}


def _depth(candidate, side, contracts):
    depths = candidate.get("production_shadow_depth") or {}
    by_side = depths.get(side) or {}
    return by_side.get(str(int(contracts))) or by_side.get(int(contracts)) or {}


def _arm(candidate, side, contracts, price):
    fee = kalshi_order_fee(
        price,
        contracts,
        schedule=candidate.get("fee_schedule") or {},
        liquidity_role="taker",
    )
    fee_dollars = _number(fee.get("fee_dollars"))
    return {
        "side": side,
        "contracts": contracts,
        "entry_price_cents": round(price, 4),
        "fee_dollars": round(fee_dollars, 6),
        "fee_schedule": fee,
        "principal_dollars": round(contracts * price / 100.0, 6),
        "total_cost_dollars": round(contracts * price / 100.0 + fee_dollars, 6),
        "result": None,
        "virtual_profit": None,
    }


def _attempt(
    ledger,
    candidate,
    hypothesis,
    confirm_fill,
    config,
    current,
    *,
    trigger=None,
):
    lane = hypothesis["lane"]
    side = hypothesis["side"]
    contracts = max(1, _integer(hypothesis.get("contracts"), 1))
    ticker = str(candidate.get("ticker") or "")
    signal_price = _entry_price(candidate, side)
    reasons = []
    initial_depth = _depth(candidate, side, contracts)
    if _number(initial_depth.get("available_contracts")) + 1e-9 < contracts:
        reasons.append("insufficient_signal_snapshot_depth")
    try:
        confirmed = confirm_fill(ticker, side, contracts) or {}
    except Exception as exc:
        confirmed = {
            "ok": False,
            "error": "confirmation_quote_unavailable",
            "message": type(exc).__name__,
        }
    confirmed_price_raw = confirmed.get("full_size_entry_price_cents")
    decision_time = _parse_time(confirmed.get("checked_at")) or current
    reasons.extend(confirmation_reasons(confirmed, config["maximum_quote_age_seconds"], decision_time))
    confirmed_price = (
        _number(confirmed_price_raw, -1.0)
        if confirmed_price_raw is not None
        else -1.0
    )
    if confirmed.get("error"):
        reasons.append(str(confirmed.get("error")))
    if confirmed_price <= 0:
        reasons.append("no_confirmed_executable_price")
    band = "settlement" if lane == SETTLEMENT_DISTANCE else "spot" if lane in {SPOT_NO_CHASE, SPOT_FLOW, SPOT_PULLBACK} else "flow"
    if not config[band + "_minimum_price_cents"] <= confirmed_price <= config[band + "_maximum_price_cents"]:
        reasons.append("confirmed_price_outside_registered_band")
    if _number(confirmed.get("available_contracts")) + 1e-9 < contracts:
        reasons.append("insufficient_confirmed_fok_depth")
    adverse_move = (
        confirmed_price - signal_price
        if confirmed_price > 0 and signal_price > 0
        else None
    )
    if (
        adverse_move is not None
        and adverse_move > config["maximum_adverse_move_cents"] + 1e-9
    ):
        reasons.append("confirmation_adverse_move")
    arm = (
        _arm(candidate, side, contracts, confirmed_price)
        if confirmed_price > 0
        else None
    )
    if not arm or not (arm.get("fee_schedule") or {}).get("exact") or not (
        arm.get("fee_schedule") or {}
    ).get("authoritative"):
        reasons.append("fee_calculation_not_exact")
    reasons = list(dict.fromkeys(reasons))
    attempt = {
        "id": f"{config['policy_hash']}:{lane}:{ticker}:{current.isoformat()}",
        "version": VERSION,
        "policy_hash": config["policy_hash"],
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "lane": lane,
        "ticker": ticker,
        "asset": str(candidate.get("asset") or "").upper(),
        "side": side,
        "captured_at": current.isoformat(),
        "minutes_to_close": candidate.get("minutes_to_close"),
        "signal_strength": hypothesis.get("strength"),
        "signal_evidence": hypothesis.get("evidence") or {},
        "signal_entry_price_cents": round(signal_price, 4),
        "confirmed_entry_price_cents": (
            round(confirmed_price, 4) if confirmed_price > 0 else None
        ),
        "adverse_move_cents": (
            round(adverse_move, 4) if adverse_move is not None else None
        ),
        "required_contracts": contracts,
        "available_contracts": confirmed.get("available_contracts"),
        "fetched_at": confirmed.get("fetched_at"),
        "would_submit": not reasons,
        "status": "would_submit" if not reasons else "rejected",
        "reason": reasons[0] if reasons else "production_parity_fillable",
        "reasons": reasons,
        "pullback_trigger": trigger,
    }
    ledger["attempts"].append(attempt)
    if reasons:
        return attempt, None
    record = {
        **attempt,
        "id": f"{config['policy_hash']}:{lane}:{ticker}",
        "status": "open",
        "event_ticker": candidate.get("event_ticker"),
        "series_ticker": candidate.get("series_ticker"),
        "close_time": candidate.get("close_time"),
        "expiry_key": candidate.get("close_time"),
        "market_kind": candidate.get("market_kind"),
        "floor_strike": candidate.get("floor_strike"),
        "cap_strike": candidate.get("cap_strike"),
        "data_quality": candidate.get("data_quality"),
        "fresh_input_snapshot": {
            "coinbase": {
                key: _venue(candidate, "coinbase").get(key)
                for key in (
                    "stream_age_seconds",
                    "trade_flow_60s",
                    "trade_flow_300s",
                    "mid_return_15s_bps",
                    "mid_return_60s_bps",
                )
            },
            "kraken": {
                key: _venue(candidate, "kraken").get(key)
                for key in (
                    "stream_age_seconds",
                    "trade_flow_60s",
                    "trade_flow_300s",
                    "mid_return_15s_bps",
                    "mid_return_60s_bps",
                )
            },
            "kalshi": {
                key: (candidate.get("kalshi_microstructure") or {}).get(key)
                for key in (
                    "source",
                    "age_seconds",
                    "sequence_valid",
                    "spread_yes_cents",
                    "yes_mid_change_60s_pp",
                    "trade_flow_60s",
                )
            },
        },
        "fill_model": "fresh_visible_taker_fok_depth",
        "arm": arm,
        "market_result": None,
        "settlement_checks": 0,
    }
    ledger["records"].append(record)
    return attempt, record


def _update_pullbacks(ledger, candidate, spot, config, current):
    if not spot:
        return
    ticker = str(candidate.get("ticker") or "")
    side = str(spot.get("side") or "")
    key = f"{ticker}:{side}"
    if any(str(row.get("key") or "") == key for row in ledger["pending_pullbacks"]):
        return
    price = _entry_price(candidate, side)
    ledger["pending_pullbacks"].append(
        {
            "key": key,
            "ticker": ticker,
            "asset": str(candidate.get("asset") or "").upper(),
            "side": side,
            "triggered_at": current.isoformat(),
            "not_before": (
                current + timedelta(seconds=config["pullback_wait_seconds"])
            ).isoformat(),
            "expires_at": (
                current + timedelta(seconds=config["pullback_expiry_seconds"])
            ).isoformat(),
            "trigger_price_cents": round(price, 4),
            "signal_strength": spot.get("strength"),
            "policy_hash": config["policy_hash"],
        }
    )


def capture_candidates(
    ledger,
    candidates,
    refresh_snapshot,
    confirm_fill,
    settings=None,
    now=None,
):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    current = _now(now)
    if not config["enabled"]:
        return [], []
    known = {
        (str(row.get("lane") or ""), str(row.get("ticker") or ""))
        for row in ledger["records"]
        if row.get("policy_hash") == config["policy_hash"]
    }
    added = []
    attempts = []
    funnel = Counter(reviewed=0, fresh_snapshots=0, quality_eligible=0)
    refreshed_by_ticker = {}
    for raw in candidates or []:
        funnel["reviewed"] += 1
        ticker = str(raw.get("ticker") or "")
        try:
            fresh = refresh_snapshot(raw) or {}
        except Exception as exc:
            fresh = {"error": f"{type(exc).__name__}: {exc}"[:300]}
        if fresh.get("error"):
            funnel[f"reason:{fresh.get('error')}"] += 1
            continue
        refreshed_by_ticker[ticker] = fresh
        funnel["fresh_snapshots"] += 1
        quality = _quality_review(fresh, config)
        if not quality["ok"]:
            for reason in quality["reasons"]:
                funnel[f"reason:{reason}"] += 1
            continue
        funnel["quality_eligible"] += 1
        hypotheses, spot = _hypotheses(fresh, config)
        _update_pullbacks(ledger, fresh, spot, config, current)
        for hypothesis in hypotheses:
            lane = hypothesis["lane"]
            if (lane, ticker) in known:
                funnel[f"already_tracked:{lane}"] += 1
                continue
            funnel[f"signal_matched:{lane}"] += 1
            attempt, record = _attempt(
                ledger,
                fresh,
                hypothesis,
                confirm_fill,
                config,
                current,
            )
            attempts.append(attempt)
            if record:
                added.append(record)
                known.add((lane, ticker))
                funnel[f"captured:{lane}"] += 1
            else:
                funnel[f"rejected:{lane}:{attempt['reason']}"] += 1

    remaining_pullbacks = []
    for pending in ledger["pending_pullbacks"]:
        expires = _parse_time(pending.get("expires_at"))
        not_before = _parse_time(pending.get("not_before"))
        ticker = str(pending.get("ticker") or "")
        if not expires or current > expires:
            funnel["pullback_expired"] += 1
            continue
        if not_before and current < not_before:
            remaining_pullbacks.append(pending)
            continue
        if (SPOT_PULLBACK, ticker) in known:
            continue
        fresh = refreshed_by_ticker.get(ticker)
        if not fresh:
            remaining_pullbacks.append(pending)
            continue
        signals = signal_reviews(fresh)
        spot = signals.get("spot_leads_kalshi") or {}
        if str(spot.get("side") or "") != str(pending.get("side") or ""):
            if spot:
                funnel["pullback_direction_changed"] += 1
                continue
            remaining_pullbacks.append(pending)
            continue
        current_price = _entry_price(fresh, pending["side"])
        target = _number(pending.get("trigger_price_cents")) - config["pullback_cents"]
        if current_price > target + 1e-9:
            remaining_pullbacks.append(pending)
            continue
        if not (
            config["spot_minimum_price_cents"]
            <= current_price
            <= config["spot_maximum_price_cents"]
        ):
            remaining_pullbacks.append(pending)
            continue
        hypothesis = {
            "lane": SPOT_PULLBACK,
            "side": pending["side"],
            "strength": spot.get("strength"),
            "contracts": config["default_contracts"],
            "evidence": {
                "trigger_price_cents": pending.get("trigger_price_cents"),
                "pullback_price_cents": current_price,
                "pullback_cents": round(
                    _number(pending.get("trigger_price_cents")) - current_price, 4
                ),
                "signal": spot.get("evidence") or {},
            },
        }
        funnel[f"signal_matched:{SPOT_PULLBACK}"] += 1
        attempt, record = _attempt(
            ledger,
            fresh,
            hypothesis,
            confirm_fill,
            config,
            current,
            trigger=pending,
        )
        attempts.append(attempt)
        if record:
            added.append(record)
            known.add((SPOT_PULLBACK, ticker))
            funnel[f"captured:{SPOT_PULLBACK}"] += 1
        else:
            remaining_pullbacks.append(pending)
            funnel[f"rejected:{SPOT_PULLBACK}:{attempt['reason']}"] += 1
    ledger["pending_pullbacks"] = remaining_pullbacks[-2000:]
    ledger["last_capture_funnel"] = dict(funnel)
    ledger["last_capture_ids"] = [row["id"] for row in added]
    ledger["updated_at"] = current.isoformat()
    retain_records(ledger, config["maximum_records"])
    retain_records(ledger, config["maximum_attempts"], "attempts")
    return added, attempts


def _market_result(payload):
    market = (payload or {}).get("market") if isinstance(payload, dict) else None
    market = market if isinstance(market, dict) else payload if isinstance(payload, dict) else {}
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()
    finalized = status in {"settled", "finalized"} and result in {
        "yes",
        "no",
    }
    return finalized, result, market.get("settlement_ts") or market.get("settled_time")


def settle_records(ledger, fetch_market, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    current = _now(now)
    checks = 0
    settled = []
    cache = {}
    for record in ledger["records"]:
        if record.get("status") != "open":
            continue
        close = _parse_time(record.get("close_time"))
        if not close or current < close + timedelta(
            minutes=config["settlement_grace_minutes"]
        ):
            continue
        if checks >= config["maximum_settlement_checks_per_scan"]:
            break
        ticker = str(record.get("ticker") or "")
        if ticker not in cache:
            try:
                cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                cache[ticker] = {"error": type(exc).__name__}
            checks += 1
        finalized, result, settled_at = _market_result(cache[ticker])
        record["settlement_checks"] = _integer(record.get("settlement_checks")) + 1
        record["last_settlement_check_at"] = current.isoformat()
        if not finalized:
            continue
        arm = record.get("arm") or {}
        won = result == str(arm.get("side") or "")
        payout = _integer(arm.get("contracts")) if won else 0.0
        arm["result"] = "WIN" if won else "LOSS"
        arm["won"] = won
        arm["virtual_profit"] = round(
            payout - _number(arm.get("total_cost_dollars")), 6
        )
        record["market_result"] = result
        record["status"] = "settled"
        record["settled_at"] = settled_at or current.isoformat()
        settled.append(record)
    ledger["last_settled_ids"] = [row["id"] for row in settled]
    if settled:
        ledger["updated_at"] = current.isoformat()
    return settled


def _cluster_lower(rows, cluster_getter):
    clusters = defaultdict(float)
    for row in rows:
        key = str(cluster_getter(row) or "")
        if key:
            clusters[key] += _number((row.get("arm") or {}).get("virtual_profit"))
    values = list(clusters.values())
    if len(values) < 2:
        return {"clusters": len(values), "lower_bound": None}
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    return {
        "clusters": len(values),
        "mean_cluster_profit": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "lower_bound": round(mean - ONE_SIDED_90_Z * standard_error, 6),
        "confidence_level": 0.90,
    }


def _lane_summary(ledger, lane, config):
    records = [
        row
        for row in ledger["records"]
        if row.get("lane") == lane and row.get("policy_hash") == config["policy_hash"]
    ]
    settled = sorted(
        [row for row in records if row.get("status") == "settled"],
        key=lambda row: str(row.get("captured_at") or ""),
    )
    attempts = [
        row
        for row in ledger["attempts"]
        if row.get("lane") == lane and row.get("policy_hash") == config["policy_hash"]
    ]
    cost = sum(_number((row.get("arm") or {}).get("total_cost_dollars")) for row in settled)
    profit = sum(_number((row.get("arm") or {}).get("virtual_profit")) for row in settled)
    wins = sum((row.get("arm") or {}).get("result") == "WIN" for row in settled)
    split = len(settled) // 2
    first_profit = sum(
        _number((row.get("arm") or {}).get("virtual_profit")) for row in settled[:split]
    )
    second_profit = sum(
        _number((row.get("arm") or {}).get("virtual_profit")) for row in settled[split:]
    )
    days = {
        _parse_time(row.get("captured_at")).astimezone(CHICAGO).date().isoformat()
        for row in settled
        if _parse_time(row.get("captured_at"))
    }
    expiry_inference = _cluster_lower(
        settled, lambda row: row.get("expiry_key") or row.get("close_time")
    )
    day_inference = _cluster_lower(
        settled,
        lambda row: (
            _parse_time(row.get("captured_at")).astimezone(CHICAGO).date().isoformat()
            if _parse_time(row.get("captured_at"))
            else None
        ),
    )
    rejection_reasons = Counter(
        str(row.get("reason") or "unknown")
        for row in attempts
        if not row.get("would_submit")
    )
    gates = {
        "minimum_unique_markets": len({row.get("ticker") for row in settled})
        >= config["minimum_review_markets"],
        "minimum_observation_days": len(days) >= config["minimum_review_days"],
        "profitable_after_fees": profit > 0,
        "both_chronological_halves_profitable": bool(split)
        and first_profit > 0
        and second_profit > 0,
        "positive_expiry_cluster_lower_bound": expiry_inference.get("lower_bound")
        is not None
        and expiry_inference["lower_bound"] > 0,
        "positive_day_cluster_lower_bound": day_inference.get("lower_bound")
        is not None
        and day_inference["lower_bound"] > 0,
    }
    return {
        "lane": lane,
        "description": LANE_DESCRIPTIONS[lane],
        "attempts": len(attempts),
        "would_submit": sum(bool(row.get("would_submit")) for row in attempts),
        "rejected": sum(not bool(row.get("would_submit")) for row in attempts),
        "rejection_reasons": dict(rejection_reasons.most_common()),
        "tracked": len(records),
        "open": sum(row.get("status") == "open" for row in records),
        "settled": len(settled),
        "unique_markets": len({row.get("ticker") for row in settled}),
        "observation_days": len(days),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(100.0 * wins / len(settled), 2) if settled else 0.0,
        "cost": round(cost, 4),
        "profit": round(profit, 4),
        "roi": round(100.0 * profit / cost, 2) if cost else 0.0,
        "chronological_halves": {
            "first_profit": round(first_profit, 4),
            "second_profit": round(second_profit, 4),
        },
        "expiry_cluster_inference": expiry_inference,
        "day_cluster_inference": day_inference,
        "review": {
            "eligible": bool(gates) and all(gates.values()),
            "gates": gates,
            "automatic_promotion": False,
        },
    }


def summarize(ledger, settings=None):
    ledger = normalize_ledger(ledger, settings)
    config = ledger["configuration"]
    lanes = [_lane_summary(ledger, lane, config) for lane in LANES]
    ranked = sorted(
        lanes,
        key=lambda row: (row["profit"], row["roi"], row["settled"]),
        reverse=True,
    )
    return {
        "version": VERSION,
        "mode": MODE if config["enabled"] else "disabled",
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "registered_at": ledger.get("registered_at"),
        "updated_at": ledger.get("updated_at"),
        "fill_model": config["fill_model"],
        "warning": (
            "Only fresh visible-depth taker/FOK simulations are scored; no raw "
            "decision-snapshot result counts as production evidence."
        ),
        "configuration": config,
        "tracked": sum(row["tracked"] for row in lanes),
        "open": sum(row["open"] for row in lanes),
        "settled": sum(row["settled"] for row in lanes),
        "pending_pullbacks": len(ledger["pending_pullbacks"]),
        "captured_this_scan": len(ledger.get("last_capture_ids") or []),
        "settled_this_scan": len(ledger.get("last_settled_ids") or []),
        "last_capture_funnel": dict(ledger.get("last_capture_funnel") or {}),
        "lanes": lanes,
        "ranking": [
            {
                "lane": row["lane"],
                "settled": row["settled"],
                "profit": row["profit"],
                "roi": row["roi"],
                "would_submit": row["would_submit"],
                "review_eligible": row["review"]["eligible"],
            }
            for row in ranked
        ],
        "recent_records": sorted(
            [
                row
                for row in ledger["records"]
                if row.get("policy_hash") == config["policy_hash"]
            ],
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
        "recent_attempts": sorted(
            [
                row
                for row in ledger["attempts"]
                if row.get("policy_hash") == config["policy_hash"]
            ],
            key=lambda row: str(row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }
