"""Guarded rollout for overnight baseball and limited-overs cricket.

NPB/KBO pregame moneylines are evaluated in shadow until a chronological
train/validation sample demonstrates positive counterfactual returns,
reasonable calibration, valid independent pricing, and non-negative CLV.
Promoted segments remain capped at one unit.  Derivative markets, cricket,
and any overseas in-game candidate stay shadow-only until their data paths
are separately validated.
"""

from __future__ import annotations

import math
from datetime import datetime

from kalshi_common import normalize_text


ROLLOUT_VERSION = "sports-overseas-rollout-v1"
STATE_VERSION = "sports-overseas-rollout-state-v1"
ROLLOUT_SKIP_REASONS = {
    "overseas_moneyline_shadow_validation",
    "overseas_totals_shadow_only",
    "overseas_live_state_unavailable",
    "cricket_shadow_only",
    "test_cricket_disabled",
    "table_tennis_disabled_no_consensus",
}


def empty_state():
    return {
        "version": STATE_VERSION,
        "rollout_version": ROLLOUT_VERSION,
        "generated_at": None,
        "segments": {},
        "transitions": [],
    }


def _number(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _sport(candidate):
    return str(candidate.get("sport_key") or candidate.get("sport") or "").strip().lower()


def _market(candidate):
    return normalize_text(candidate.get("market_type") or candidate.get("bet_type") or "").replace(" ", "_")


def rollout_segment(candidate):
    sport = _sport(candidate)
    market = _market(candidate)
    if sport in {"baseball_npb", "baseball_kbo"} and market == "moneyline":
        return f"{sport}|moneyline|pregame"
    return ""


def candidate_policy(candidate):
    sport = _sport(candidate)
    market = _market(candidate)
    started = bool(candidate.get("game_started"))
    if sport.startswith(("table_tennis", "tabletennis", "ping_pong")):
        return "disabled", "table_tennis_disabled_no_consensus", ""
    if sport == "cricket_test_match":
        return "disabled", "test_cricket_disabled", ""
    if sport.startswith("cricket_"):
        if started:
            return "shadow", "overseas_live_state_unavailable", ""
        return "shadow", "cricket_shadow_only", ""
    if sport in {"baseball_npb", "baseball_kbo"}:
        if started:
            return "shadow", "overseas_live_state_unavailable", ""
        if market == "total":
            return "shadow", "overseas_totals_shadow_only", ""
        if market == "moneyline":
            return "validation", "overseas_moneyline_shadow_validation", rollout_segment(candidate)
    return "standard", "", ""


def _decision_probability(row):
    pricing = row.get("pricing_v2") or {}
    probability = _number(
        pricing.get("calibrated_ensemble_probability"),
        _number(pricing.get("fair_probability")),
    )
    if probability is None:
        return None
    if probability > 1:
        probability /= 100.0
    return max(0.001, min(0.999, probability))


def _performance(rows):
    profits = [
        value for row in rows
        if (value := _number(row.get("hypothetical_profit_units"))) is not None
    ]
    probabilities = []
    identity_passes = 0
    clv = []
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for row in rows:
        probability = _decision_probability(row)
        if probability is not None and row.get("result") in {"WIN", "LOSS"}:
            outcome = 1.0 if row.get("result") == "WIN" else 0.0
            probabilities.append((probability, outcome))
        if int(_number(row.get("independent_book_family_count"), 0) or 0) >= 1:
            identity_passes += 1
        clv_value = _number(row.get("shadow_clv_5m_cents"))
        if clv_value is not None:
            clv.append(clv_value)
        profit = _number(row.get("hypothetical_profit_units"))
        if profit is not None:
            equity += profit
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
    profit_units = sum(profits)
    return {
        "event_count": len(rows),
        "profit_units": round(profit_units, 4),
        "roi_pct": round(100.0 * profit_units / len(profits), 2) if profits else None,
        "max_drawdown_units": round(max_drawdown, 4),
        "brier": round(
            sum((probability - outcome) ** 2 for probability, outcome in probabilities)
            / len(probabilities),
            4,
        ) if probabilities else None,
        "identity_valid_rate": round(identity_passes / len(rows), 4) if rows else None,
        "average_clv_5m_cents": round(sum(clv) / len(clv), 3) if clv else None,
        "clv_5m_count": len(clv),
    }


def _deduplicated_eligible_rows(observations, segment):
    latest = {}
    for row in observations or []:
        if rollout_segment(row) != segment or bool(row.get("game_started")):
            continue
        reasons = set(row.get("skip_reasons") or [])
        if reasons - ROLLOUT_SKIP_REASONS:
            continue
        if not row.get("would_execute_without_overseas_rollout", not bool(reasons - ROLLOUT_SKIP_REASONS)):
            continue
        ticker = str(row.get("kalshi_ticker") or "")
        side = normalize_text(row.get("order_side") or "yes")
        if not ticker:
            continue
        key = f"{ticker}|{side}"
        if str(row.get("generated_at") or "") >= str((latest.get(key) or {}).get("generated_at") or ""):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: str(row.get("generated_at") or ""))


def update_state(
    resolved_observations,
    state=None,
    *,
    enabled=True,
    auto_promote=True,
    min_train_events=18,
    min_validation_events=12,
):
    state = state if isinstance(state, dict) else empty_state()
    state["version"] = STATE_VERSION
    state["rollout_version"] = ROLLOUT_VERSION
    segments = state.setdefault("segments", {})
    transitions = state.setdefault("transitions", [])
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for sport in ("baseball_npb", "baseball_kbo"):
        segment = f"{sport}|moneyline|pregame"
        rows = _deduplicated_eligible_rows(resolved_observations, segment)
        required = int(min_train_events) + int(min_validation_events)
        selected = rows[-required:] if len(rows) >= required else rows
        train = selected[: int(min_train_events)]
        validation = selected[int(min_train_events):]
        train_metrics = _performance(train)
        validation_metrics = _performance(validation)
        blockers = []
        if len(train) < int(min_train_events):
            blockers.append("insufficient_train_events")
        if len(validation) < int(min_validation_events):
            blockers.append("insufficient_validation_events")
        if train_metrics.get("roi_pct") is None or train_metrics["roi_pct"] <= 0:
            blockers.append("train_roi_not_positive")
        if validation_metrics.get("roi_pct") is None or validation_metrics["roi_pct"] < 2.0:
            blockers.append("validation_roi_below_2pct")
        if validation_metrics.get("max_drawdown_units", 999) > 6.0:
            blockers.append("validation_drawdown_above_6u")
        brier = validation_metrics.get("brier")
        if brier is None or brier > 0.26:
            blockers.append("validation_brier_above_0_26")
        identity_rate = validation_metrics.get("identity_valid_rate")
        if identity_rate is None or identity_rate < 0.95:
            blockers.append("validation_identity_rate_below_95pct")
        if (
            validation_metrics.get("clv_5m_count", 0) >= 5
            and validation_metrics.get("average_clv_5m_cents", -999) < -0.5
        ):
            blockers.append("validation_clv_below_minus_0_5c")
        previous = segments.get(segment) or {}
        status = str(previous.get("status") or "shadow")
        if enabled and auto_promote and not blockers:
            status = "promoted_1u"
        if status != previous.get("status"):
            transitions.append({
                "at": now,
                "segment": segment,
                "from": previous.get("status") or "unseen",
                "to": status,
                "blockers": blockers,
            })
        segments[segment] = {
            "status": status if enabled else "disabled",
            "execution_active": bool(enabled and status == "promoted_1u"),
            "max_units": 1,
            "eligible_event_count": len(rows),
            "required_train_events": int(min_train_events),
            "required_validation_events": int(min_validation_events),
            "train": train_metrics,
            "validation": validation_metrics,
            "blockers": blockers,
            "updated_at": now,
        }
    state["transitions"] = transitions[-100:]
    state["generated_at"] = now
    state["enabled"] = bool(enabled)
    state["auto_promote"] = bool(auto_promote)
    return state


def apply_candidate(candidate, state=None, *, enabled=True, auto_promote=True):
    policy, reason, segment = candidate_policy(candidate)
    segment_state = ((state or {}).get("segments") or {}).get(segment) or {}
    promoted = bool(
        enabled
        and auto_promote
        and policy == "validation"
        and segment_state.get("execution_active")
    )
    # Disabling the rollout is fail-closed: overseas candidates remain shadow
    # observations instead of silently falling through to live execution.
    if reason and not promoted:
        candidate.setdefault("skip_reasons", []).append(reason)
        candidate["skip_reasons"] = list(dict.fromkeys(candidate["skip_reasons"]))
    review = {
        "version": ROLLOUT_VERSION,
        "enabled": bool(enabled),
        "policy": policy,
        "segment": segment,
        "status": "promoted_1u" if promoted else segment_state.get("status") or policy,
        "execution_active": promoted,
        "max_units": 1 if policy == "validation" else 0,
        "reason": "" if promoted else reason,
        "metrics": {
            "eligible_event_count": segment_state.get("eligible_event_count", 0),
            "train": segment_state.get("train") or {},
            "validation": segment_state.get("validation") or {},
            "blockers": segment_state.get("blockers") or [],
        },
    }
    candidate["overseas_rollout"] = review
    return review


def summary(candidates, state=None):
    rows = [candidate.get("overseas_rollout") or {} for candidate in candidates or []]
    relevant = [row for row in rows if row.get("policy") != "standard"]
    return {
        "version": ROLLOUT_VERSION,
        "enabled": bool((state or {}).get("enabled", True)),
        "auto_promote": bool((state or {}).get("auto_promote", True)),
        "candidate_count": len(relevant),
        "shadow_candidate_count": sum(1 for row in relevant if not row.get("execution_active")),
        "execution_active_count": sum(1 for row in relevant if row.get("execution_active")),
        "segments": (state or {}).get("segments") or {},
        "transitions": list((state or {}).get("transitions") or [])[-10:],
    }
