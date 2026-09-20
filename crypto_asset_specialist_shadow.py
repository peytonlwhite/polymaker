"""Forward-only production-parity ETH/XRP specialist strategy lab.

These hypotheses were registered after the broad execution lab showed a stable
asset split: ETH flow and selected XRP flow variants outperformed while BTC,
SOL, and DOGE did not.  Historical execution-lab records are deliberately not
backfilled.  Every entry must survive the shared fresh-book, visible-depth,
exact-fee, second-quote path used by the production-parity execution lab.

This module cannot place orders, mutate bankroll state, recover losses, or
promote a strategy automatically.
"""

from __future__ import annotations
from crypto_execution_safety import confirmation_reasons
from crypto_evidence import retain_records

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

from crypto_execution_lab_shadow import (
    _arm as build_exact_arm,
    _depth as production_depth,
    _entry_price as entry_price,
    _market_result as official_market_result,
    _quality_review as production_quality_review,
    _same_persistent_flow as persistent_flow,
    configuration as execution_lab_configuration,
)
from crypto_signal_tournament_shadow import signal_reviews


VERSION = "crypto-asset-specialist-shadow-v1"
MODE = "production_pipeline_shadow_only"
CHICAGO = ZoneInfo("America/Chicago")

ETH_CORE = "eth_flow_core_forward"
ETH_LATE = "eth_flow_late_forward"
ETH_NO = "eth_flow_no_forward"
XRP_PERSISTENCE = "xrp_flow_persistence_forward"
XRP_LATE = "xrp_flow_late_forward"
PORTFOLIO = "eth_xrp_deduplicated_ensemble"

LANES = (
    ETH_CORE,
    ETH_LATE,
    ETH_NO,
    XRP_PERSISTENCE,
    XRP_LATE,
    PORTFOLIO,
)

LANE_DESCRIPTIONS = {
    ETH_CORE: "ETH-only replication of fresh Coinbase/Kraken 60-second flow agreement.",
    ETH_LATE: "ETH flow replication restricted to 10-13 minutes before close.",
    ETH_NO: "ETH-only NO-side flow replication registered after the broad asset review.",
    XRP_PERSISTENCE: "XRP flow must agree across Coinbase/Kraken and 60s/300s horizons.",
    XRP_LATE: "XRP 60-second cross-venue flow restricted to 10-13 minutes before close.",
    PORTFOLIO: "One ETH/XRP position per ticker; five contracts only for cross-horizon agreement.",
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


def _policy_hash(config):
    payload = {key: value for key, value in config.items() if key != "policy_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def configuration(settings=None):
    settings = settings or {}
    base = execution_lab_configuration(settings)
    family_alpha = max(
        0.001,
        min(
            0.20,
            _number(
                settings.get("CRYPTO_ASSET_SPECIALIST_FAMILY_ALPHA", 0.05),
                0.05,
            ),
        ),
    )
    confidence_level = 1.0 - family_alpha / len(LANES)
    config = {
        "enabled": _boolean(
            settings.get("CRYPTO_ASSET_SPECIALIST_SHADOW_ENABLED", True), True
        ),
        "assets": ["ETH", "XRP"],
        "minimum_minutes": base["minimum_minutes"],
        "maximum_minutes": base["maximum_minutes"],
        "minimum_data_quality": base["minimum_data_quality"],
        "maximum_quote_age_seconds": base["maximum_quote_age_seconds"],
        "maximum_source_age_seconds": base["maximum_source_age_seconds"],
        "maximum_spread_cents": base["maximum_spread_cents"],
        "maximum_adverse_move_cents": base["maximum_adverse_move_cents"],
        "flow_minimum_strength": base["flow_minimum_strength"],
        "flow_minimum_price_cents": base["flow_minimum_price_cents"],
        "flow_maximum_price_cents": base["flow_maximum_price_cents"],
        "late_minimum_minutes": base["flow_late_minimum_minutes"],
        "persistent_flow_threshold": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_ASSET_SPECIALIST_PERSISTENCE_THRESHOLD", 0.10
                ),
                0.10,
            ),
        ),
        "default_contracts": base["default_contracts"],
        "confirmed_contracts": base["confirmed_contracts"],
        "settlement_grace_minutes": base["settlement_grace_minutes"],
        "maximum_settlement_checks_per_scan": base[
            "maximum_settlement_checks_per_scan"
        ],
        "minimum_review_markets": max(
            50,
            _integer(
                settings.get("CRYPTO_ASSET_SPECIALIST_MIN_REVIEW_MARKETS", 100),
                100,
            ),
        ),
        "minimum_review_days": max(
            3,
            _integer(
                settings.get("CRYPTO_ASSET_SPECIALIST_MIN_REVIEW_DAYS", 7), 7
            ),
        ),
        "maximum_records": max(
            1000,
            _integer(
                settings.get("CRYPTO_ASSET_SPECIALIST_MAX_RECORDS", 10000),
                10000,
            ),
        ),
        "maximum_attempts": max(
            1000,
            _integer(
                settings.get("CRYPTO_ASSET_SPECIALIST_MAX_ATTEMPTS", 10000),
                10000,
            ),
        ),
        "family_alpha": family_alpha,
        "family_hypotheses": len(LANES),
        "per_lane_confidence_level": confidence_level,
        "historical_backfill": False,
        "execution_revision": "crypto-audit-2026-09-v1",
        "affects_execution": False,
        "automatic_promotion": False,
        "fill_model": "fresh_visible_taker_fok_depth",
        "portfolio_lane": PORTFOLIO,
        "lanes": list(LANES),
    }
    config["policy_hash"] = _policy_hash(config)
    return config


def empty_ledger(settings=None, now=None):
    current = _now(now).isoformat()
    config = configuration(settings)
    return {
        "version": VERSION,
        "mode": MODE,
        "registered_at": current,
        "policy_registered_at": current,
        "updated_at": current,
        "configuration": config,
        "policy_history": [],
        "records": [],
        "attempts": [],
        "last_capture_funnel": {},
        "last_capture_ids": [],
        "last_settled_ids": [],
    }


def normalize_ledger(value, settings=None, now=None):
    ledger = value if isinstance(value, dict) else {}
    current = _now(now).isoformat()
    old_config = ledger.get("configuration") or {}
    config = configuration(settings)
    ledger["version"] = VERSION
    ledger["mode"] = MODE
    ledger.setdefault("registered_at", current)
    ledger.setdefault("policy_registered_at", ledger["registered_at"])
    ledger["updated_at"] = ledger.get("updated_at") or ledger["registered_at"]
    ledger["policy_history"] = (
        ledger.get("policy_history")
        if isinstance(ledger.get("policy_history"), list)
        else []
    )
    if (
        old_config.get("policy_hash")
        and old_config.get("policy_hash") != config["policy_hash"]
    ):
        ledger["policy_history"].append(
            {
                "changed_at": current,
                "from_policy_hash": old_config.get("policy_hash"),
                "to_policy_hash": config["policy_hash"],
            }
        )
        ledger["policy_registered_at"] = current
    ledger["configuration"] = config
    for key in ("records", "attempts"):
        ledger[key] = ledger.get(key) if isinstance(ledger.get(key), list) else []
    ledger.setdefault("last_capture_funnel", {})
    ledger.setdefault("last_capture_ids", [])
    ledger.setdefault("last_settled_ids", [])
    ledger["policy_history"] = ledger["policy_history"][-100:]
    return ledger


def _flow_hypothesis(candidate, config):
    flow = (signal_reviews(candidate).get("cross_venue_flow_follow") or {})
    side = str(flow.get("side") or "")
    price = entry_price(candidate, side) if side else -1.0
    if not (
        flow
        and _number(flow.get("strength")) >= config["flow_minimum_strength"]
        and config["flow_minimum_price_cents"]
        <= price
        <= config["flow_maximum_price_cents"]
    ):
        return None
    return {
        "side": side,
        "strength": flow.get("strength"),
        "evidence": flow.get("evidence") or {},
    }


def _persistent_hypothesis(candidate, config):
    direction, evidence = persistent_flow(
        candidate, threshold=config["persistent_flow_threshold"]
    )
    if not direction:
        return None
    side = "yes" if direction > 0 else "no"
    price = entry_price(candidate, side)
    if not (
        config["flow_minimum_price_cents"]
        <= price
        <= config["flow_maximum_price_cents"]
    ):
        return None
    return {
        "side": side,
        "strength": min(abs(_number(value)) for value in evidence.values()),
        "evidence": evidence,
    }


def hypotheses(candidate, config):
    asset = str(candidate.get("asset") or "").upper()
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    flow = _flow_hypothesis(candidate, config)
    persistence = _persistent_hypothesis(candidate, config)
    rows = []

    if asset == "ETH" and flow:
        base = {
            **flow,
            "contracts": config["default_contracts"],
            "signal_tags": ["eth", "cross_venue_flow_60s"],
        }
        rows.append({"lane": ETH_CORE, **base})
        if minutes >= config["late_minimum_minutes"]:
            rows.append(
                {
                    "lane": ETH_LATE,
                    **base,
                    "signal_tags": base["signal_tags"] + ["late_window"],
                }
            )
        if flow["side"] == "no":
            rows.append(
                {
                    "lane": ETH_NO,
                    **base,
                    "signal_tags": base["signal_tags"] + ["no_side"],
                }
            )

        ensemble_votes = ["cross_venue_flow_60s"]
        cross_horizon = bool(
            persistence and persistence.get("side") == flow.get("side")
        )
        if cross_horizon:
            ensemble_votes.append("cross_venue_flow_60s_300s")
        if minutes >= config["late_minimum_minutes"]:
            ensemble_votes.append("late_window")
        rows.append(
            {
                "lane": PORTFOLIO,
                "side": flow["side"],
                "strength": flow["strength"],
                "contracts": (
                    config["confirmed_contracts"]
                    if cross_horizon
                    else config["default_contracts"]
                ),
                "evidence": {
                    "votes": ensemble_votes,
                    "flow": flow["evidence"],
                    "persistence": (
                        persistence.get("evidence") if cross_horizon else None
                    ),
                },
                "signal_tags": ["eth", "deduplicated_portfolio"]
                + ensemble_votes,
                "portfolio_candidate": True,
            }
        )

    if asset == "XRP":
        if persistence:
            rows.append(
                {
                    "lane": XRP_PERSISTENCE,
                    **persistence,
                    "contracts": config["default_contracts"],
                    "signal_tags": ["xrp", "cross_venue_flow_60s_300s"],
                }
            )
        late = bool(flow and minutes >= config["late_minimum_minutes"])
        if late:
            rows.append(
                {
                    "lane": XRP_LATE,
                    **flow,
                    "contracts": config["default_contracts"],
                    "signal_tags": ["xrp", "cross_venue_flow_60s", "late_window"],
                }
            )
        aligned = bool(
            persistence
            and late
            and persistence.get("side") == flow.get("side")
        )
        selected = persistence or (flow if late else None)
        conflict = bool(
            persistence
            and late
            and persistence.get("side") != flow.get("side")
        )
        if selected and not conflict:
            votes = []
            if persistence:
                votes.append("cross_venue_flow_60s_300s")
            if late:
                votes.append("late_window_flow_60s")
            rows.append(
                {
                    "lane": PORTFOLIO,
                    "side": selected["side"],
                    "strength": selected["strength"],
                    "contracts": (
                        config["confirmed_contracts"]
                        if aligned
                        else config["default_contracts"]
                    ),
                    "evidence": {
                        "votes": votes,
                        "flow": flow.get("evidence") if flow else None,
                        "persistence": (
                            persistence.get("evidence") if persistence else None
                        ),
                    },
                    "signal_tags": ["xrp", "deduplicated_portfolio"] + votes,
                    "portfolio_candidate": True,
                }
            )
    return rows


def _attempt(ledger, candidate, hypothesis, confirm_fill, config, current):
    lane = hypothesis["lane"]
    side = hypothesis["side"]
    contracts = max(1, _integer(hypothesis.get("contracts"), 1))
    ticker = str(candidate.get("ticker") or "")
    signal_price = entry_price(candidate, side)
    reasons = []
    initial_depth = production_depth(candidate, side, contracts)
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
    confirmed_raw = confirmed.get("full_size_entry_price_cents")
    decision_time = _parse_time(confirmed.get("checked_at")) or current
    reasons.extend(confirmation_reasons(confirmed, config["maximum_quote_age_seconds"], decision_time))
    confirmed_price = (
        _number(confirmed_raw, -1.0) if confirmed_raw is not None else -1.0
    )
    if confirmed.get("error"):
        reasons.append(str(confirmed.get("error")))
    if confirmed_price <= 0:
        reasons.append("no_confirmed_executable_price")
    if not config["flow_minimum_price_cents"] <= confirmed_price <= config["flow_maximum_price_cents"]:
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
        build_exact_arm(candidate, side, contracts, confirmed_price)
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
        "signal_tags": hypothesis.get("signal_tags") or [],
        "portfolio_candidate": bool(hypothesis.get("portfolio_candidate")),
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
            source: {
                key: ((candidate.get("microstructure") or {}).get(source) or {}).get(
                    key
                )
                for key in (
                    "stream_age_seconds",
                    "trade_flow_60s",
                    "trade_flow_300s",
                    "mid_return_15s_bps",
                    "mid_return_60s_bps",
                )
            }
            for source in ("coinbase", "kraken")
        },
        "kalshi_input_snapshot": {
            key: (candidate.get("kalshi_microstructure") or {}).get(key)
            for key in (
                "source",
                "age_seconds",
                "sequence_valid",
                "spread_yes_cents",
                "trade_flow_60s",
                "yes_mid_change_60s_pp",
            )
        },
        "fill_model": config["fill_model"],
        "arm": arm,
        "market_result": None,
        "settlement_checks": 0,
    }
    ledger["records"].append(record)
    return attempt, record


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
    for raw in candidates or []:
        funnel["reviewed"] += 1
        try:
            fresh = refresh_snapshot(raw) or {}
        except Exception as exc:
            fresh = {
                "error": "production_shadow_snapshot_exception",
                "message": type(exc).__name__,
            }
        if fresh.get("error"):
            funnel[f"reason:{fresh.get('error')}"] += 1
            continue
        funnel["fresh_snapshots"] += 1
        quality = production_quality_review(fresh, config)
        if not quality["ok"]:
            for reason in quality["reasons"]:
                funnel[f"reason:{reason}"] += 1
            continue
        funnel["quality_eligible"] += 1
        rows = hypotheses(fresh, config)
        if not rows:
            funnel["reason:no_registered_specialist_signal"] += 1
        for hypothesis in rows:
            lane = hypothesis["lane"]
            ticker = str(fresh.get("ticker") or "")
            funnel[f"signal_matched:{lane}"] += 1
            if (lane, ticker) in known:
                funnel[f"already_tracked:{lane}"] += 1
                continue
            attempt, record = _attempt(
                ledger, fresh, hypothesis, confirm_fill, config, current
            )
            attempts.append(attempt)
            if record:
                added.append(record)
                known.add((lane, ticker))
                funnel[f"captured:{lane}"] += 1
            else:
                funnel[f"rejected:{lane}:{attempt['reason']}"] += 1
    ledger["last_capture_funnel"] = dict(funnel)
    ledger["last_capture_ids"] = [row["id"] for row in added]
    ledger["updated_at"] = current.isoformat()
    retain_records(ledger, config["maximum_records"])
    retain_records(ledger, config["maximum_attempts"], "attempts")
    return added, attempts


def settle_records(ledger, fetch_market, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    current = _now(now)
    settled = []
    payloads = {}
    checks = 0
    grace = timedelta(minutes=config["settlement_grace_minutes"])
    for record in ledger["records"]:
        if record.get("status") != "open" or record.get("policy_hash") != config[
            "policy_hash"
        ]:
            continue
        close_time = _parse_time(record.get("close_time"))
        if not close_time or current < close_time + grace:
            continue
        ticker = str(record.get("ticker") or "")
        if ticker not in payloads:
            if checks >= config["maximum_settlement_checks_per_scan"]:
                break
            try:
                payloads[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                payloads[ticker] = {
                    "error": "settlement_fetch_unavailable",
                    "message": type(exc).__name__,
                }
            checks += 1
        record["settlement_checks"] = _integer(record.get("settlement_checks")) + 1
        record["last_settlement_check_at"] = current.isoformat()
        finalized, result, settled_at = official_market_result(payloads[ticker])
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


def _cluster_lower(rows, cluster_getter, confidence_level):
    clusters = defaultdict(float)
    for row in rows:
        key = str(cluster_getter(row) or "")
        if key:
            clusters[key] += _number((row.get("arm") or {}).get("virtual_profit"))
    values = list(clusters.values())
    if len(values) < 2:
        return {
            "clusters": len(values),
            "lower_bound": None,
            "confidence_level": confidence_level,
        }
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    z_value = NormalDist().inv_cdf(confidence_level)
    return {
        "clusters": len(values),
        "mean_cluster_profit": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "lower_bound": round(mean - z_value * standard_error, 6),
        "confidence_level": round(confidence_level, 8),
    }


def _lane_summary(ledger, lane, config):
    records = [
        row
        for row in ledger["records"]
        if row.get("lane") == lane
        and row.get("policy_hash") == config["policy_hash"]
    ]
    settled = sorted(
        [row for row in records if row.get("status") == "settled"],
        key=lambda row: str(row.get("captured_at") or ""),
    )
    attempts = [
        row
        for row in ledger["attempts"]
        if row.get("lane") == lane
        and row.get("policy_hash") == config["policy_hash"]
    ]
    cost = sum(
        _number((row.get("arm") or {}).get("total_cost_dollars"))
        for row in settled
    )
    profit = sum(
        _number((row.get("arm") or {}).get("virtual_profit")) for row in settled
    )
    wins = sum((row.get("arm") or {}).get("result") == "WIN" for row in settled)
    split = len(settled) // 2
    first_profit = sum(
        _number((row.get("arm") or {}).get("virtual_profit"))
        for row in settled[:split]
    )
    second_profit = sum(
        _number((row.get("arm") or {}).get("virtual_profit"))
        for row in settled[split:]
    )
    days = {
        _parse_time(row.get("captured_at")).astimezone(CHICAGO).date().isoformat()
        for row in settled
        if _parse_time(row.get("captured_at"))
    }
    confidence = config["per_lane_confidence_level"]
    expiry_inference = _cluster_lower(
        settled,
        lambda row: row.get("expiry_key") or row.get("close_time"),
        confidence,
    )
    day_inference = _cluster_lower(
        settled,
        lambda row: (
            _parse_time(row.get("captured_at"))
            .astimezone(CHICAGO)
            .date()
            .isoformat()
            if _parse_time(row.get("captured_at"))
            else None
        ),
        confidence,
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
    rejection_reasons = Counter(
        str(row.get("reason") or "unknown")
        for row in attempts
        if not row.get("would_submit")
    )
    return {
        "lane": lane,
        "description": LANE_DESCRIPTIONS[lane],
        "portfolio_candidate": lane == PORTFOLIO,
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
        "contracts": sum(_integer((row.get("arm") or {}).get("contracts")) for row in settled),
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


def summarize(ledger, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    lanes = [_lane_summary(ledger, lane, config) for lane in LANES]
    ranking = sorted(
        lanes,
        key=lambda row: (row["profit"], row["roi"], row["settled"]),
        reverse=True,
    )
    registered = _parse_time(ledger.get("policy_registered_at"))
    updated = _parse_time(ledger.get("updated_at")) or _now(now)
    observation_hours = (
        max(0.0, (updated - registered).total_seconds() / 3600.0)
        if registered
        else 0.0
    )
    portfolio = next(row for row in lanes if row["lane"] == PORTFOLIO)
    portfolio_submits = portfolio["would_submit"]
    return {
        "version": VERSION,
        "mode": MODE if config["enabled"] else "disabled",
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "registered_at": ledger.get("registered_at"),
        "policy_registered_at": ledger.get("policy_registered_at"),
        "updated_at": ledger.get("updated_at"),
        "fill_model": config["fill_model"],
        "familywise_confidence_level": round(1.0 - config["family_alpha"], 6),
        "per_lane_confidence_level": round(
            config["per_lane_confidence_level"], 6
        ),
        "warning": (
            "Post-hoc asset findings are tested only from policy registration forward. "
            "The de-duplicated portfolio lane is the only projected live-volume lane."
        ),
        "configuration": config,
        "tracked": sum(row["tracked"] for row in lanes),
        "open": sum(row["open"] for row in lanes),
        "settled": sum(row["settled"] for row in lanes),
        "captured_this_scan": len(ledger.get("last_capture_ids") or []),
        "settled_this_scan": len(ledger.get("last_settled_ids") or []),
        "last_capture_funnel": dict(ledger.get("last_capture_funnel") or {}),
        "portfolio_projection": {
            "lane": PORTFOLIO,
            "would_submit": portfolio_submits,
            "settled": portfolio["settled"],
            "contracts": portfolio["contracts"],
            "observation_hours": round(observation_hours, 4),
            "positions_per_hour": (
                round(portfolio_submits / observation_hours, 4)
                if observation_hours > 0
                else 0.0
            ),
            "exact_fill_rate": (
                round(portfolio_submits / portfolio["attempts"], 6)
                if portfolio["attempts"]
                else 0.0
            ),
            "one_position_per_ticker": True,
            "maximum_contracts": config["confirmed_contracts"],
        },
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
            for row in ranking
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
