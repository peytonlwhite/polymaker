"""Bounded, evidence-driven adaptive controls for the sports strategy.

The controller never increases risk above the checked-in baseline.  It can
only leave a segment active, cap it at one unit, or route it to shadow mode.
"""

from __future__ import annotations

import json
import math
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kalshi_common import normalize_text


ADAPTIVE_VERSION = "sports-adaptive-v2-broad-segments"
ADAPTIVE_SHADOW_REASON = "adaptive_strategy_shadow"
STATE_RANK = {"active": 0, "probation": 1, "shadow": 2}

DEFAULT_POLICY = {
    "enabled": True,
    "window_events": 60,
    "transition_cooldown_hours": 24,
    # Exact segments in this list remain at the checked-in active baseline.
    # This is intentionally narrow: other matching price/edge/detail segments
    # can still enter probation or shadow when their own evidence deteriorates.
    "forced_active_segments": [],
    "active_to_probation": {
        "min_events": 20,
        "max_pnl_dollars": -150.0,
        "max_pnl_units": -5.0,
        "max_roi_pct": -5.0,
        "max_clv_5m_cents": -0.5,
    },
    "active_to_shadow": {
        "min_events": 30,
        "max_pnl_dollars": -300.0,
        "max_pnl_units": -10.0,
        "max_roi_pct": -8.0,
        "max_clv_5m_cents": -1.0,
    },
    "probation_to_shadow": {
        "min_events": 15,
        "max_pnl_units": -3.0,
        "max_roi_pct": -3.0,
        "max_clv_5m_cents": -0.5,
    },
    "probation_to_active": {
        "min_events": 15,
        "min_pnl_units": 1.0,
        "min_roi_pct": 0.0,
        "min_clv_5m_cents": 0.0,
    },
    "shadow_to_probation": {
        "min_events": 30,
        "min_pnl_units": 3.0,
        "min_roi_pct": 3.0,
        "min_clv_5m_cents": 0.0,
    },
    "probation_max_units": 1,
}


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().astimezone().isoformat(timespec="seconds")


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # Bot state historically stores local wall-clock timestamps without an
        # offset. Interpret those in the host timezone instead of UTC.
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc)


def empty_state(policy=None):
    return {
        "version": ADAPTIVE_VERSION,
        "enabled": True,
        "generated_at": iso_now(),
        "policy": {**DEFAULT_POLICY, **(policy or {})},
        "segments": {},
        "audit_log": [],
    }


def load_state(path):
    path = Path(path)
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return empty_state()
    if not isinstance(state, dict):
        return empty_state()
    state.setdefault("version", ADAPTIVE_VERSION)
    state.setdefault("enabled", True)
    state["policy"] = {**DEFAULT_POLICY, **(state.get("policy") or {})}
    state.setdefault("segments", {})
    state.setdefault("audit_log", [])
    return state


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    backup = path.with_suffix(path.suffix + ".lastgood")
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary.write_text(encoded, encoding="utf-8")
    if path.exists():
        backup.write_bytes(path.read_bytes())
    os.replace(temporary, path)


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def price_bucket(value):
    price = _number(value)
    if price is None or price <= 0 or price >= 100:
        return "unknown"
    lower = min(90, int(price // 10) * 10)
    upper = lower + 9
    return f"{lower:02d}-{upper:02d}c"


def edge_bucket(value):
    edge = _number(value)
    if edge is None:
        return "unknown"
    if edge < 0:
        return "negative"
    if edge < 2:
        return "0-2pp"
    if edge < 4:
        return "2-4pp"
    if edge < 6:
        return "4-6pp"
    return "6pp-plus"


def timing_bucket(row):
    explicit = normalize_text(
        row.get("bet_timing_bucket")
        or row.get("timing_bucket")
        or (row.get("pricing_v2") or {}).get("timing_bucket")
    ).replace(" ", "_")
    if explicit:
        return explicit
    return "live" if row.get("game_started") else "pregame"


def row_entry_price(row):
    pricing = row.get("pricing_v2") or {}
    for value in (row.get("entry_price"), pricing.get("ask_cents")):
        price = _number(value)
        if price is not None and 0 < price < 100:
            return price
    return None


def segment_keys(row):
    bucket = price_bucket(row_entry_price(row))
    sport = normalize_text(row.get("sport_key") or row.get("sport") or "unknown").replace(" ", "_")
    market = normalize_text(row.get("market_type") or row.get("bet_type") or "unknown").replace(" ", "_")
    timing = timing_bucket(row)
    pricing = row.get("pricing_v2") or {}
    edge = edge_bucket(
        row.get("edge")
        if row.get("edge") is not None
        else row.get("net_edge")
        if row.get("net_edge") is not None
        else pricing.get("execution_edge_pp")
    )
    keys = [f"sport_market:{sport}|{market}"]
    if edge != "unknown":
        keys.extend([
            f"edge:{edge}",
            f"sport_market_edge:{sport}|{market}|{edge}",
        ])
    if bucket != "unknown":
        keys.extend([
            f"price:{bucket}",
            f"detail:{sport}|{market}|{timing}|{bucket}",
        ])
    return keys


def _event_key(row, index=0):
    return str(
        row.get("game_key")
        or row.get("event_id")
        or row.get("kalshi_event_ticker")
        or row.get("kalshi_ticker")
        or f"row-{index}"
    )


def _five_minute_clv(row):
    direct = _number(row.get("shadow_clv_5m_cents"))
    if direct is not None:
        return direct
    horizon = (row.get("fixed_horizon_clv") or {}).get("5m") or {}
    return _number(horizon.get("clv_vs_entry_ask_cents"))


def _executed_sample(row, index):
    result = str(row.get("result") or "").upper()
    if result not in {"WIN", "LOSS"} or not isinstance(row.get("pricing_v2"), dict):
        return None
    if row.get("trusted_capper") or str(row.get("source") or "") == "trusted_capper":
        return None
    if (row.get("sports_game_odds_strategy") or {}).get("active"):
        return None
    if str(row.get("source") or "edge_scanner") not in {"", "edge_scanner"}:
        return None
    price = row_entry_price(row)
    profit = _number(row.get("profit"))
    stake = _number(row.get("stake"), 0.0)
    if price is None or profit is None or stake <= 0:
        return None
    units = row.get("sports_units") or {}
    unit_size = _number(row.get("unit_size") or units.get("unit_size"))
    profit_units = profit / unit_size if unit_size and unit_size > 0 else profit / stake
    return {
        "kind": "executed",
        "event_key": _event_key(row, index),
        "ticker": row.get("kalshi_ticker"),
        "at": row.get("settled_at") or row.get("placed_at") or row.get("created_at"),
        "profit_dollars": profit,
        "profit_units": profit_units,
        "stake_dollars": stake,
        "clv_5m_cents": _five_minute_clv(row),
        "segments": segment_keys(row),
    }


def _shadow_sample(row, index):
    result = str(row.get("result") or "").upper()
    if result not in {"WIN", "LOSS"} or not row.get("would_execute_without_adaptive"):
        return None
    if normalize_text(row.get("adaptive_state_at_decision")) != "shadow":
        return None
    profit_units = _number(row.get("hypothetical_profit_units"))
    price = row_entry_price(row)
    if profit_units is None or price is None:
        return None
    return {
        "kind": "shadow",
        "event_key": _event_key(row, index),
        "ticker": row.get("kalshi_ticker"),
        "at": row.get("settled_at") or row.get("generated_at"),
        "profit_dollars": profit_units,
        "profit_units": profit_units,
        "stake_dollars": 1.0,
        "clv_5m_cents": _five_minute_clv(row),
        "segments": segment_keys(row),
    }


def collect_samples(portfolio, candidate_registry):
    samples = []
    executed_tickers = set()
    rows = [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]
    for index, row in enumerate(rows):
        sample = _executed_sample(row, index)
        if not sample:
            continue
        samples.append(sample)
        if sample.get("ticker"):
            executed_tickers.add(str(sample["ticker"]))
    for index, row in enumerate((candidate_registry or {}).get("resolved_observations") or []):
        if str(row.get("kalshi_ticker") or "") in executed_tickers:
            continue
        sample = _shadow_sample(row, index)
        if sample:
            samples.append(sample)
    return samples


def _samples_after(samples, value):
    cutoff = parse_time(value)
    if not cutoff:
        return list(samples)
    return [sample for sample in samples if (parse_time(sample.get("at")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]


def segment_metrics(samples, *, max_events=60):
    grouped = defaultdict(list)
    for sample in samples:
        grouped[sample["event_key"]].append(sample)
    ordered = sorted(
        grouped.items(),
        key=lambda item: max(
            parse_time(row.get("at")) or datetime.min.replace(tzinfo=timezone.utc)
            for row in item[1]
        ),
    )
    if max_events > 0:
        ordered = ordered[-max_events:]
    selected = [row for _event, event_rows in ordered for row in event_rows]
    event_returns = [sum(_number(row.get("profit_units"), 0.0) for row in event_rows) for _event, event_rows in ordered]
    pnl_dollars = sum(_number(row.get("profit_dollars"), 0.0) for row in selected)
    pnl_units = sum(_number(row.get("profit_units"), 0.0) for row in selected)
    stake = sum(_number(row.get("stake_dollars"), 0.0) for row in selected)
    clv_values = [value for row in selected if (value := _number(row.get("clv_5m_cents"))) is not None]
    upper_90 = None
    if event_returns:
        mean_return = statistics.fmean(event_returns)
        standard_error = (
            statistics.stdev(event_returns) / math.sqrt(len(event_returns))
            if len(event_returns) >= 2
            else 0.0
        )
        upper_90 = mean_return + 1.645 * standard_error
    return {
        "event_count": len(ordered),
        "observation_count": len(selected),
        "pnl_dollars": round(pnl_dollars, 2),
        "pnl_units": round(pnl_units, 3),
        "stake_dollars": round(stake, 2),
        "roi_pct": round(pnl_dollars / stake * 100.0, 2) if stake > 0 else None,
        "average_clv_5m_cents": round(statistics.fmean(clv_values), 3) if clv_values else None,
        "clv_5m_count": len(clv_values),
        "mean_event_return_units": round(statistics.fmean(event_returns), 4) if event_returns else None,
        "upper_90_event_return_units": round(upper_90, 4) if upper_90 is not None else None,
    }


def _negative_evidence(metrics, clv_threshold):
    upper = metrics.get("upper_90_event_return_units")
    clv = metrics.get("average_clv_5m_cents")
    return bool(
        (upper is not None and upper < 0)
        or (clv is not None and clv <= float(clv_threshold))
    )


def _active_transition(metrics, policy):
    severe = policy["active_to_shadow"]
    if (
        metrics["event_count"] >= int(severe["min_events"])
        and (
            metrics["pnl_dollars"] <= float(severe["max_pnl_dollars"])
            or metrics["pnl_units"] <= float(severe["max_pnl_units"])
        )
        and metrics.get("roi_pct") is not None
        and metrics["roi_pct"] <= float(severe["max_roi_pct"])
        and _negative_evidence(metrics, severe["max_clv_5m_cents"])
    ):
        return "shadow", "active_drawdown_confirmed"
    probation = policy["active_to_probation"]
    if (
        metrics["event_count"] >= int(probation["min_events"])
        and (
            metrics["pnl_dollars"] <= float(probation["max_pnl_dollars"])
            or metrics["pnl_units"] <= float(probation["max_pnl_units"])
        )
        and metrics.get("roi_pct") is not None
        and metrics["roi_pct"] <= float(probation["max_roi_pct"])
        and _negative_evidence(metrics, probation["max_clv_5m_cents"])
    ):
        return "probation", "active_weakness_confirmed"
    return "active", "within_active_policy"


def _probation_transition(metrics, policy):
    down = policy["probation_to_shadow"]
    if (
        metrics["event_count"] >= int(down["min_events"])
        and metrics["pnl_units"] <= float(down["max_pnl_units"])
        and metrics.get("roi_pct") is not None
        and metrics["roi_pct"] <= float(down["max_roi_pct"])
        and _negative_evidence(metrics, down["max_clv_5m_cents"])
    ):
        return "shadow", "probation_failed"
    up = policy["probation_to_active"]
    clv = metrics.get("average_clv_5m_cents")
    if (
        metrics["event_count"] >= int(up["min_events"])
        and metrics["pnl_units"] >= float(up["min_pnl_units"])
        and metrics.get("roi_pct") is not None
        and metrics["roi_pct"] > float(up["min_roi_pct"])
        and clv is not None
        and clv >= float(up["min_clv_5m_cents"])
    ):
        return "active", "probation_passed"
    return "probation", "probation_collecting_evidence"


def _shadow_transition(metrics, policy):
    up = policy["shadow_to_probation"]
    clv = metrics.get("average_clv_5m_cents")
    if (
        metrics["event_count"] >= int(up["min_events"])
        and metrics["pnl_units"] >= float(up["min_pnl_units"])
        and metrics.get("roi_pct") is not None
        and metrics["roi_pct"] >= float(up["min_roi_pct"])
        and clv is not None
        and clv >= float(up["min_clv_5m_cents"])
    ):
        return "probation", "shadow_requalified"
    return "shadow", "shadow_collecting_evidence"


def evaluate_state(
    portfolio,
    candidate_registry,
    state=None,
    *,
    generated_at=None,
    policy_overrides=None,
):
    state = json.loads(json.dumps(state or empty_state()))
    now = parse_time(generated_at) or utc_now()
    generated_at = (now.astimezone()).isoformat(timespec="seconds")
    policy = {
        **DEFAULT_POLICY,
        **(state.get("policy") or {}),
        **(policy_overrides or {}),
    }
    forced_active_segments = {
        str(value or "").strip()
        for value in (policy.get("forced_active_segments") or [])
        if str(value or "").strip()
    }
    policy["forced_active_segments"] = sorted(forced_active_segments)
    state["policy"] = policy
    state["enabled"] = bool(state.get("enabled", policy.get("enabled", True)))
    samples = collect_samples(portfolio or {}, candidate_registry or {})
    by_segment = defaultdict(list)
    for sample in samples:
        for key in sample.get("segments") or []:
            by_segment[key].append(sample)
    all_keys = sorted(set(by_segment).union((state.get("segments") or {}).keys()))
    transitions = []
    evaluated = {}
    for key in all_keys:
        previous = dict((state.get("segments") or {}).get(key) or {})
        current = normalize_text(previous.get("state") or "active")
        if current not in STATE_RANK:
            current = "active"
        forced_active = key in forced_active_segments
        relevant_kind = "executed" if forced_active else "shadow" if current == "shadow" else "executed"
        relevant = [row for row in by_segment.get(key, []) if row.get("kind") == relevant_kind]
        if current in {"shadow", "probation"} and not forced_active:
            relevant = _samples_after(relevant, previous.get("state_changed_at"))
        metrics = segment_metrics(relevant, max_events=int(policy.get("window_events") or 60))
        if forced_active:
            proposed, reason = "active", "forced_active_policy"
        elif current == "active":
            proposed, reason = _active_transition(metrics, policy)
        elif current == "probation":
            proposed, reason = _probation_transition(metrics, policy)
        else:
            proposed, reason = _shadow_transition(metrics, policy)
        changed_at = parse_time(previous.get("state_changed_at"))
        cooldown = timedelta(hours=float(policy.get("transition_cooldown_hours") or 0))
        cooldown_active = bool(changed_at and now - changed_at < cooldown)
        if proposed != current and cooldown_active and not forced_active:
            proposed, reason = current, "transition_cooldown_active"
        entry = {
            **previous,
            "state": proposed,
            "reason": reason,
            "forced_active": forced_active,
            "last_evaluated_at": generated_at,
            "metrics": metrics,
        }
        if proposed != current:
            entry["state_changed_at"] = generated_at
            transition = {
                "at": generated_at,
                "segment": key,
                "from": current,
                "to": proposed,
                "reason": reason,
                "metrics": metrics,
            }
            transitions.append(transition)
        elif not entry.get("state_changed_at") and proposed != "active":
            entry["state_changed_at"] = generated_at
        evaluated[key] = entry
    state["segments"] = evaluated
    state["audit_log"] = [*(state.get("audit_log") or []), *transitions][-500:]
    state["generated_at"] = generated_at
    state["version"] = ADAPTIVE_VERSION
    counts = defaultdict(int)
    for row in evaluated.values():
        counts[row.get("state") or "active"] += 1
    state["summary"] = {
        "segment_counts": dict(counts),
        "transition_count": len(transitions),
        "sample_count": len(samples),
        "executed_sample_count": sum(row.get("kind") == "executed" for row in samples),
        "shadow_sample_count": sum(row.get("kind") == "shadow" for row in samples),
    }
    return state, transitions


def candidate_review(candidate, state):
    if not state or not state.get("enabled", True):
        return {"enabled": False, "state": "active", "ok": True, "matching_segments": []}
    forced_active_segments = {
        str(value or "").strip()
        for value in ((state.get("policy") or {}).get("forced_active_segments") or [])
        if str(value or "").strip()
    }
    matches = []
    for key in segment_keys(candidate):
        entry = (state.get("segments") or {}).get(key) or {}
        status = normalize_text(entry.get("state") or "active")
        if status not in STATE_RANK:
            status = "active"
        forced_active = key in forced_active_segments
        if forced_active:
            status = "active"
        matches.append({
            "segment": key,
            "state": status,
            "reason": "forced_active_policy" if forced_active else entry.get("reason"),
            "forced_active": forced_active,
            "state_changed_at": entry.get("state_changed_at"),
        })
    effective = max((row["state"] for row in matches), key=lambda value: STATE_RANK[value], default="active")
    return {
        "enabled": True,
        "state": effective,
        "ok": effective != "shadow",
        "max_units": 1 if effective == "probation" else None,
        "matching_segments": matches,
        "forced_active_segments": sorted(
            row["segment"] for row in matches if row.get("forced_active")
        ),
        "state_version": state.get("version"),
        "state_generated_at": state.get("generated_at"),
    }


def public_summary(state, limit=20):
    segments = []
    for key, entry in (state.get("segments") or {}).items():
        if entry.get("state") == "active" and (entry.get("metrics") or {}).get("event_count", 0) < 10:
            continue
        segments.append({"segment": key, **entry})
    segments.sort(
        key=lambda row: (
            STATE_RANK.get(row.get("state"), 0),
            -float((row.get("metrics") or {}).get("pnl_units") or 0),
        ),
        reverse=True,
    )
    return {
        "version": state.get("version"),
        "enabled": state.get("enabled", True),
        "generated_at": state.get("generated_at"),
        "summary": state.get("summary") or {},
        "forced_active_segments": list(
            (state.get("policy") or {}).get("forced_active_segments") or []
        ),
        "active_overrides": [row for row in segments if row.get("state") != "active"],
        "reviewed_segments": segments[:limit],
    }
