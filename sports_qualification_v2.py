"""Evidence-first sports qualification challenger with guarded promotion.

Qualification V2 is deliberately evaluated for every priced candidate while
the legacy strategy remains authoritative.  A sport/market segment can only
affect execution after independently settled, chronological shadow results pass
the promotion gates below. Promotion starts at a shadow-only 0.5-unit rescue
tier and every higher half-unit tier must prove itself separately.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from datetime import datetime

from kalshi_common import normalize_text


QUALIFICATION_VERSION = "sports-qualification-v2"
STATE_VERSION = "sports-qualification-promotion-v1"

# These are legacy *proxies* which V2 is designed to replace after promotion.
# Price validity, independent pricing, state, liquidity and execution controls
# are intentionally absent and can never be removed by this module.
LEGACY_PROXY_REASONS = {
    "below_edge",
    "live_edge_too_low",
    "low_confidence",
    "live_confidence_too_low",
    "spread_confidence_too_low",
    "spread_pro_score_too_low",
    "spread_final_score_too_low",
}

# These failures describe the quality of the candidate itself.  Portfolio and
# timing constraints (open slots, exposure, duplicate position, etc.) remain
# execution blockers but do not contaminate counterfactual outcome evaluation.
QUALITY_HARD_REASONS = {
    "market_type_disabled",
    "trusted_capper_only_sport",
    "low_kalshi_volume",
    "low_kalshi_liquidity",
    "entry_price_too_low",
    "wide_or_missing_price",
    "live_score_context_missing",
    "authoritative_live_derivative_state_required",
    "same_book_consensus_unavailable",
    "kalshi_stream_snapshot_required",
}

TIER_REQUIREMENTS = {
    0.5: {
        "min_evidence_score": 60.0,
        "min_p_edge_positive": 0.60,
        "min_robust_price_room_cents": 0.25,
        "min_book_families": 1,
        "min_sync_score": 30.0,
    },
    1: {
        "min_evidence_score": 64.0,
        "min_p_edge_positive": 0.65,
        "min_robust_price_room_cents": 0.50,
        "min_book_families": 1,
        "min_sync_score": 35.0,
    },
    1.5: {
        "min_evidence_score": 68.0,
        "min_p_edge_positive": 0.685,
        "min_robust_price_room_cents": 0.875,
        "min_book_families": 2,
        "min_sync_score": 37.5,
    },
    2: {
        "min_evidence_score": 72.0,
        "min_p_edge_positive": 0.72,
        "min_robust_price_room_cents": 1.25,
        "min_book_families": 2,
        "min_sync_score": 40.0,
    },
    2.5: {
        "min_evidence_score": 75.5,
        "min_p_edge_positive": 0.76,
        "min_robust_price_room_cents": 1.75,
        "min_book_families": 2,
        "min_sync_score": 45.0,
    },
    3: {
        "min_evidence_score": 79.0,
        "min_p_edge_positive": 0.80,
        "min_robust_price_room_cents": 2.25,
        "min_book_families": 2,
        "min_sync_score": 50.0,
    },
    3.5: {
        "min_evidence_score": 82.5,
        "min_p_edge_positive": 0.84,
        "min_robust_price_room_cents": 2.875,
        "min_book_families": 3,
        "min_sync_score": 52.5,
    },
    4: {
        "min_evidence_score": 86.0,
        "min_p_edge_positive": 0.88,
        "min_robust_price_room_cents": 3.50,
        "min_book_families": 3,
        "min_sync_score": 55.0,
    },
    4.5: {
        "min_evidence_score": 89.0,
        "min_p_edge_positive": 0.91,
        "min_robust_price_room_cents": 4.25,
        "min_book_families": 3,
        "min_sync_score": 60.0,
    },
    5: {
        "min_evidence_score": 92.0,
        "min_p_edge_positive": 0.94,
        "min_robust_price_room_cents": 5.00,
        "min_book_families": 3,
        "min_sync_score": 65.0,
    },
}

CONFIG_HASH = hashlib.sha256(
    json.dumps(
        {
            "version": QUALIFICATION_VERSION,
            "tiers": TIER_REQUIREMENTS,
            "formula": "posterior30+room20+books12+sync10+micro8+agreement8+cross5+sharp4+proxy3",
            "promotion_gates": {
                "min_roi_pct": 2.0,
                "min_rescue_roi_pct": 1.0,
                "max_drawdown_units": 6.0,
                "max_brier": 0.26,
                "max_ece": 0.12,
                "max_log_loss": 0.75,
                "min_clv_cents_when_available": -0.50,
                "current_strategy_roi_tolerance_pp": 3.0,
                "current_strategy_brier_tolerance": 0.03,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()[:16]


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clamp(value, low, high):
    return max(float(low), min(float(high), float(value)))


def _unit_key(value):
    number = round(float(value or 0), 3)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def _unit_steps():
    return sorted(float(value) for value in TIER_REQUIREMENTS)


def _normalized_sport(value):
    sport = str(value or "unknown").strip().lower()
    if sport.startswith("tennis_atp"):
        return "tennis_atp"
    if sport.startswith("tennis_wta"):
        return "tennis_wta"
    return sport or "unknown"


def qualification_segment(candidate):
    return f"{_normalized_sport(candidate.get('sport_key'))}|{normalize_text(candidate.get('market_type')) or 'unknown'}"


def empty_state():
    return {
        "version": STATE_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "config_hash": CONFIG_HASH,
        "generated_at": None,
        "segments": {},
        "transitions": [],
    }


def _current_price_evidence(candidate):
    pricing = candidate.get("pricing_v2") or {}
    distribution = pricing.get("probability_distribution") or {}
    card_price = ((candidate.get("bet_intelligence") or {}).get("price_quality") or {})
    entry = _number(candidate.get("entry_price"), _number(pricing.get("ask_cents")))
    fee = _number(candidate.get("estimated_fee_edge_pp"), 0.0)
    slippage = _number(distribution.get("estimated_slippage_cents"), 0.0)
    robust = _number(distribution.get("robust_probability"))
    all_in = _number(distribution.get("all_in_price_cents"))
    if all_in is None and entry is not None:
        all_in = entry + fee + slippage
    room = robust - all_in if robust is not None and all_in is not None else None
    if room is None:
        room = _number(card_price.get("price_room_to_positive_ev_ceiling_cents"))
    conservative = _number(
        pricing.get("net_conservative_edge_pp"),
        _number(card_price.get("conservative_net_edge_pp"), _number(candidate.get("edge"))),
    )
    p_edge = _number(
        distribution.get("posterior_p_edge_positive"),
        _number(pricing.get("posterior_p_edge_positive"), _number(pricing.get("p_edge_positive"))),
    )
    if p_edge is not None and p_edge > 1.0:
        p_edge /= 100.0
    probability = _number(
        distribution.get("median_probability"),
        _number(pricing.get("fair_probability"), _number(candidate.get("model_prob"))),
    )
    return {
        "entry_price_cents": entry,
        "all_in_price_cents": all_in,
        "robust_probability": robust,
        "robust_price_room_cents": room,
        "conservative_net_edge_pp": conservative,
        "posterior_p_edge_positive": p_edge,
        "decision_probability": probability,
    }


def _independent_model_range(candidate):
    disagreement = ((candidate.get("bet_intelligence") or {}).get("model_disagreement") or {})
    probabilities = disagreement.get("probabilities") or {}
    ignored = {"kalshi_market", "game_state_shadow"}
    values = [
        number
        for key, value in probabilities.items()
        if str(key or "").strip().lower() not in ignored
        and (number := _number(value)) is not None
    ]
    if len(values) < 2:
        return None, len(values)
    return max(values) - min(values), len(values)


def _quality_hard_reason(reason):
    reason = str(reason or "")
    return bool(
        reason in QUALITY_HARD_REASONS
        or reason.startswith("pricing_v2_")
        or reason.startswith("identity_")
    )


def _tier_passes(metrics, tier):
    requirement = TIER_REQUIREMENTS[tier]
    return bool(
        metrics["evidence_score"] + 1e-9 >= requirement["min_evidence_score"]
        and metrics["posterior_p_edge_positive"] + 1e-9 >= requirement["min_p_edge_positive"]
        and metrics["robust_price_room_cents"] + 1e-9 >= requirement["min_robust_price_room_cents"]
        and metrics["independent_book_families"] >= requirement["min_book_families"]
        and metrics["source_sync_score"] + 1e-9 >= requirement["min_sync_score"]
    )


def evaluate_candidate(candidate, promotion_state=None, *, enabled=True, auto_promote=True):
    """Return an immutable counterfactual V2 decision for one candidate."""
    pricing = candidate.get("pricing_v2") or {}
    card = candidate.get("bet_intelligence") or {}
    price = _current_price_evidence(candidate)
    consensus = pricing.get("consensus") or {}
    families = int(_number(
        candidate.get("independent_book_family_count"),
        _number(consensus.get("independent_family_count"), 0),
    ) or 0)
    sync = card.get("source_synchronization") or {}
    sync_score = _number(sync.get("score"), 50.0)
    micro = card.get("market_microstructure") or {}
    micro_score = _number(micro.get("quality_score"), 50.0)
    cross = card.get("cross_market_consistency") or {}
    sportsbook = card.get("sportsbook_market") or {}
    model_range, model_count = _independent_model_range(candidate)

    p_edge = price["posterior_p_edge_positive"]
    room = price["robust_price_room_cents"]
    conservative = price["conservative_net_edge_pp"]
    hard_blockers = []
    if not card.get("version"):
        hard_blockers.append("qualification_v2_intelligence_unavailable")
    if not pricing.get("ok"):
        hard_blockers.append(pricing.get("reason") or "pricing_unavailable")
    if families < 1:
        hard_blockers.append("qualification_v2_independent_book_required")
    if p_edge is None:
        hard_blockers.append("qualification_v2_posterior_edge_unavailable")
    if room is None:
        hard_blockers.append("qualification_v2_robust_price_room_unavailable")
    elif room <= 0:
        hard_blockers.append("qualification_v2_non_positive_all_in_edge")
    if conservative is None or conservative <= 0:
        hard_blockers.append("qualification_v2_non_positive_conservative_edge")
    entry = price["entry_price_cents"]
    if entry is None or not 0 < entry < 100:
        hard_blockers.append("qualification_v2_invalid_executable_price")
    if micro.get("orderbook_valid") is False:
        hard_blockers.append("qualification_v2_invalid_orderbook")
    if candidate.get("game_started") and micro.get("quote_fresh") is False:
        hard_blockers.append("qualification_v2_stale_live_quote")

    current_reasons = list(dict.fromkeys(candidate.get("skip_reasons") or []))
    proxy_blockers = [reason for reason in current_reasons if reason in LEGACY_PROXY_REASONS]
    unit_state = candidate.get("sports_units") or {}
    if unit_state:
        legacy_units = _number(
            unit_state.get("legacy_raw_target_units"),
            _number(unit_state.get("raw_target_units")),
        )
        if legacy_units is not None and legacy_units <= 0:
            proxy_blockers.append("legacy_unit_quality_filter")
    proxy_blockers = list(dict.fromkeys(proxy_blockers))
    quality_hard_blockers = [reason for reason in current_reasons if _quality_hard_reason(reason)]
    hard_blockers.extend(quality_hard_blockers)
    hard_blockers = list(dict.fromkeys(str(reason) for reason in hard_blockers if reason))
    execution_blockers = [
        reason for reason in current_reasons
        if reason not in LEGACY_PROXY_REASONS and reason not in quality_hard_blockers
    ]

    posterior_component = 0.0 if p_edge is None else _clamp((p_edge - 0.50) / 0.40 * 30.0, 0.0, 30.0)
    room_component = 0.0 if room is None else _clamp(room / 6.0 * 20.0, 0.0, 20.0)
    books_component = min(12.0, {0: 0.0, 1: 4.0, 2: 7.0, 3: 9.0, 4: 10.5}.get(families, 12.0))
    sync_component = _clamp(sync_score / 100.0 * 10.0, 0.0, 10.0)
    micro_component = _clamp(micro_score / 100.0 * 8.0, 0.0, 8.0)
    agreement_component = (
        4.0 if model_range is None
        else 8.0 if model_range <= 3.0
        else 6.0 if model_range <= 6.0
        else 3.0 if model_range <= 10.0
        else 0.0
    )
    cross_component = {
        "coherent": 5.0,
        "insufficient_sibling_markets": 3.0,
        "contradiction_detected": 0.0,
    }.get(str(cross.get("status") or ""), 2.0)
    sharp_signal = str(sportsbook.get("sharp_signal") or "")
    sharp_component = 4.0 if sharp_signal == "sharp_higher" else 2.0 if sharp_signal == "sharp_retail_aligned" else 1.0 if sharp_signal == "insufficient_sharp_retail_split" else 0.0
    confidence = _number(candidate.get("confidence_score"), 0.0)
    final_score = _number(candidate.get("final_bet_score"), 0.0)
    pro_score = _number((candidate.get("pro_review") or {}).get("score"), 0.0)
    proxy_component = _clamp(confidence / 100.0 * 1.5, 0.0, 1.5) + _clamp(max(final_score, pro_score) / 100.0 * 1.5, 0.0, 1.5)
    components = {
        "posterior_edge": round(posterior_component, 3),
        "robust_price_room": round(room_component, 3),
        "independent_books": round(books_component, 3),
        "source_synchronization": round(sync_component, 3),
        "market_microstructure": round(micro_component, 3),
        "independent_model_agreement": round(agreement_component, 3),
        "cross_market_consistency": round(cross_component, 3),
        "sharp_market_signal": round(sharp_component, 3),
        "legacy_proxy_tiebreaker": round(proxy_component, 3),
    }
    evidence_score = round(sum(components.values()), 3)
    metrics = {
        **price,
        "evidence_score": evidence_score,
        "independent_book_families": families,
        "source_sync_score": round(sync_score, 3),
        "microstructure_quality_score": round(micro_score, 3),
        "independent_model_range_pp": round(model_range, 3) if model_range is not None else None,
        "independent_model_count": model_count,
        "cross_market_status": cross.get("status"),
        "sharp_signal": sharp_signal,
        "legacy_confidence": confidence,
        "legacy_pro_score": pro_score,
        "legacy_final_score": final_score,
    }

    passed_tiers = []
    if not hard_blockers and p_edge is not None and room is not None:
        passed_tiers = [tier for tier in sorted(TIER_REQUIREMENTS) if _tier_passes(metrics, tier)]
    recommended_units = max(passed_tiers, default=0)
    threshold_blockers = []
    next_tier = next(
        (tier for tier in _unit_steps() if tier > float(recommended_units) + 1e-9),
        _unit_steps()[-1],
    )
    for name, required in TIER_REQUIREMENTS[next_tier].items():
        metric_name = {
            "min_evidence_score": "evidence_score",
            "min_p_edge_positive": "posterior_p_edge_positive",
            "min_robust_price_room_cents": "robust_price_room_cents",
            "min_book_families": "independent_book_families",
            "min_sync_score": "source_sync_score",
        }[name]
        actual = _number(metrics.get(metric_name))
        if actual is None or actual + 1e-9 < float(required):
            threshold_blockers.append({
                "metric": metric_name,
                "actual": actual,
                "required": required,
                "shortfall": round(float(required) - float(actual or 0.0), 3),
            })

    segment = qualification_segment(candidate)
    segment_state = ((promotion_state or {}).get("segments") or {}).get(segment) or {}
    promoted_max_units = float(_number(segment_state.get("promoted_max_units"), 0) or 0)
    execution_units = min(recommended_units, promoted_max_units) if enabled and auto_promote else 0
    qualifies = bool(not hard_blockers and recommended_units > 0)
    execution_active = bool(qualifies and execution_units > 0)
    return {
        "version": QUALIFICATION_VERSION,
        "config_hash": CONFIG_HASH,
        "mode": "promoted_segment" if execution_active else "shadow_only",
        "enabled": bool(enabled),
        "segment": segment,
        "qualifies": qualifies,
        "recommended_units": recommended_units,
        "passed_tiers": passed_tiers,
        "tier_requirements": {str(key): dict(value) for key, value in TIER_REQUIREMENTS.items()},
        "metrics": metrics,
        "evidence_components": components,
        "hard_blockers": hard_blockers,
        "threshold_blockers": threshold_blockers,
        "legacy_proxy_blockers": proxy_blockers,
        "legacy_execution_blockers": execution_blockers,
        "legacy_would_qualify": not bool(current_reasons),
        "rescue_candidate": bool(qualifies and proxy_blockers),
        "promotion_status": segment_state.get("status") or "shadow",
        "promoted_max_units": promoted_max_units,
        "execution_units": execution_units,
        "execution_active": execution_active,
        "affects_execution": execution_active,
        "promotion_evidence": segment_state.get("unit_gates") or {},
    }


def apply_candidate(candidate, promotion_state=None, *, enabled=True, auto_promote=True):
    """Attach V2 and remove only replaceable legacy proxies when promoted."""
    previous = candidate.get("qualification_v2") or {}
    review = evaluate_candidate(
        candidate,
        promotion_state,
        enabled=enabled,
        auto_promote=auto_promote,
    )
    prior_proxy = list(previous.get("legacy_proxy_blockers") or [])
    if prior_proxy:
        review["legacy_proxy_blockers"] = list(dict.fromkeys([
            *prior_proxy,
            *(review.get("legacy_proxy_blockers") or []),
        ]))
        review["legacy_would_qualify"] = False
        review["rescue_candidate"] = bool(
            review.get("qualifies") and review["legacy_proxy_blockers"]
        )
    candidate["qualification_v2"] = review
    if review["execution_active"]:
        before = list(candidate.get("skip_reasons") or [])
        candidate["skip_reasons"] = [
            reason for reason in before if reason not in LEGACY_PROXY_REASONS
        ]
        review["removed_legacy_proxy_reasons"] = [
            reason for reason in before if reason in LEGACY_PROXY_REASONS
        ]
    return review


def compact_qualification(candidate):
    review = candidate.get("qualification_v2") or {}
    metrics = review.get("metrics") or {}
    return {
        "version": review.get("version"),
        "config_hash": review.get("config_hash"),
        "mode": review.get("mode"),
        "segment": review.get("segment"),
        "qualifies": review.get("qualifies"),
        "recommended_units": review.get("recommended_units"),
        "evidence_score": metrics.get("evidence_score"),
        "posterior_p_edge_positive": metrics.get("posterior_p_edge_positive"),
        "robust_price_room_cents": metrics.get("robust_price_room_cents"),
        "conservative_net_edge_pp": metrics.get("conservative_net_edge_pp"),
        "decision_probability": metrics.get("decision_probability"),
        "independent_book_families": metrics.get("independent_book_families"),
        "source_sync_score": metrics.get("source_sync_score"),
        "hard_blockers": review.get("hard_blockers") or [],
        "threshold_blockers": review.get("threshold_blockers") or [],
        "legacy_proxy_blockers": review.get("legacy_proxy_blockers") or [],
        "legacy_would_qualify": review.get("legacy_would_qualify"),
        "rescue_candidate": review.get("rescue_candidate"),
        "promotion_status": review.get("promotion_status"),
        "promoted_max_units": review.get("promoted_max_units"),
        "execution_units": review.get("execution_units"),
        "execution_active": review.get("execution_active"),
        "affects_execution": review.get("affects_execution"),
    }


def _event_key(row):
    return str(
        row.get("game_key")
        or row.get("event_id")
        or row.get("kalshi_ticker")
        or "unknown"
    )


def _decision_rows(rows, segment, unit, *, rescue_only=False):
    eligible = []
    for row in rows or []:
        review = row.get("qualification_v2") or {}
        if review.get("version") != QUALIFICATION_VERSION or review.get("config_hash") != CONFIG_HASH:
            continue
        if review.get("segment") != segment or not review.get("qualifies"):
            continue
        if float(_number(review.get("recommended_units"), 0) or 0) + 1e-9 < float(unit):
            continue
        if rescue_only and not review.get("rescue_candidate"):
            continue
        if str(row.get("result") or "").upper() not in {"WIN", "LOSS"}:
            continue
        eligible.append(row)
    # One independently settled game is one observation.  At the first scan in
    # which the strategy qualified, select the highest-evidence option exactly
    # as a real-time ranker would have done.
    grouped = defaultdict(list)
    for row in eligible:
        grouped[_event_key(row)].append(row)
    selected = []
    for event_rows in grouped.values():
        first_at = min(str(row.get("generated_at") or "") for row in event_rows)
        first_scan = [row for row in event_rows if str(row.get("generated_at") or "") == first_at]
        selected.append(max(
            first_scan,
            key=lambda row: (
                _number((row.get("qualification_v2") or {}).get("evidence_score"), 0.0),
                _number((row.get("qualification_v2") or {}).get("robust_price_room_cents"), 0.0),
            ),
        ))
    return sorted(selected, key=lambda row: str(row.get("generated_at") or row.get("settled_at") or ""))


def _baseline_rows(rows, segment):
    eligible = []
    for row in rows or []:
        review = row.get("qualification_v2") or {}
        if review.get("version") != QUALIFICATION_VERSION or review.get("config_hash") != CONFIG_HASH:
            continue
        if review.get("segment") != segment:
            continue
        if not (
            review.get("legacy_would_qualify") is True
            or row.get("would_execute_without_adaptive") is True
        ):
            continue
        if str(row.get("result") or "").upper() not in {"WIN", "LOSS"}:
            continue
        eligible.append(row)
    grouped = defaultdict(list)
    for row in eligible:
        grouped[_event_key(row)].append(row)
    selected = []
    for event_rows in grouped.values():
        first_at = min(str(row.get("generated_at") or "") for row in event_rows)
        first_scan = [row for row in event_rows if str(row.get("generated_at") or "") == first_at]
        selected.append(max(
            first_scan,
            key=lambda row: (
                _number(row.get("edge"), -999.0),
                _number((row.get("qualification_v2") or {}).get("evidence_score"), 0.0),
            ),
        ))
    return sorted(selected, key=lambda row: str(row.get("generated_at") or row.get("settled_at") or ""))


def _performance(rows):
    if not rows:
        return {"event_count": 0}
    profits = [_number(row.get("hypothetical_profit_units"), 0.0) for row in rows]
    outcomes = [1.0 if str(row.get("result") or "").upper() == "WIN" else 0.0 for row in rows]
    probabilities = [
        _number((row.get("qualification_v2") or {}).get("decision_probability"))
        for row in rows
    ]
    paired = [
        (_clamp(probability / 100.0, 0.001, 0.999), outcome)
        for probability, outcome in zip(probabilities, outcomes)
        if probability is not None
    ]
    brier = sum((probability - outcome) ** 2 for probability, outcome in paired) / len(paired) if paired else None
    log_loss = -sum(
        outcome * math.log(probability) + (1.0 - outcome) * math.log(1.0 - probability)
        for probability, outcome in paired
    ) / len(paired) if paired else None
    bins = defaultdict(list)
    for probability, outcome in paired:
        bins[min(4, int(probability * 5))].append((probability, outcome))
    ece = sum(
        len(bucket) / len(paired) * abs(
            sum(row[0] for row in bucket) / len(bucket)
            - sum(row[1] for row in bucket) / len(bucket)
        )
        for bucket in bins.values()
    ) if paired else None
    running = peak = max_drawdown = 0.0
    for profit in profits:
        running += profit
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    clv = [
        value for row in rows
        if (value := _number(row.get("shadow_clv_5m_cents"))) is not None
    ]
    total_profit = sum(profits)
    return {
        "event_count": len(rows),
        "wins": int(sum(outcomes)),
        "losses": len(rows) - int(sum(outcomes)),
        "win_rate_pct": round(sum(outcomes) / len(rows) * 100.0, 2),
        "profit_units": round(total_profit, 4),
        "roi_pct": round(total_profit / len(rows) * 100.0, 2),
        "max_drawdown_units": round(max_drawdown, 4),
        "probability_count": len(paired),
        "brier": round(brier, 6) if brier is not None else None,
        "log_loss": round(log_loss, 6) if log_loss is not None else None,
        "expected_calibration_error": round(ece, 6) if ece is not None else None,
        "clv_5m_count": len(clv),
        "average_clv_5m_cents": round(sum(clv) / len(clv), 3) if clv else None,
    }


def _unit_gate(rows, segment, unit, min_train_events, min_validation_events):
    half_steps_above_one = max(0, int(round(float(unit) * 2)) - 2)
    required_train = int(min_train_events) + half_steps_above_one * 3
    required_validation = int(min_validation_events) + half_steps_above_one * 2
    selected = _decision_rows(rows, segment, unit)
    rescue = _decision_rows(rows, segment, unit, rescue_only=True)
    baseline = _baseline_rows(rows, segment)
    validation = selected[required_train:]
    rescue_validation = rescue[required_train:]
    baseline_validation = baseline[-max(required_validation, 8):]
    validation_metrics = _performance(validation)
    rescue_metrics = _performance(rescue_validation)
    baseline_metrics = _performance(baseline_validation)
    blockers = []
    if len(selected) < required_train + required_validation:
        blockers.append("insufficient_independent_events")
    if validation_metrics.get("event_count", 0) < required_validation:
        blockers.append("insufficient_walk_forward_validation_events")
    if rescue_metrics.get("event_count", 0) < max(5, required_validation // 2):
        blockers.append("insufficient_rescue_validation_events")
    if validation_metrics.get("profit_units", 0.0) <= 0 or validation_metrics.get("roi_pct", -999.0) < 2.0:
        blockers.append("walk_forward_profit_not_proven")
    if rescue_metrics.get("profit_units", 0.0) <= 0 or rescue_metrics.get("roi_pct", -999.0) < 1.0:
        blockers.append("rescue_profit_not_proven")
    if validation_metrics.get("max_drawdown_units", 999.0) > 6.0:
        blockers.append("walk_forward_drawdown_too_high")
    brier = validation_metrics.get("brier")
    if brier is None or brier > 0.26:
        blockers.append("walk_forward_brier_not_calibrated")
    ece = validation_metrics.get("expected_calibration_error")
    if ece is None or ece > 0.12:
        blockers.append("walk_forward_calibration_error_too_high")
    log_loss = validation_metrics.get("log_loss")
    if log_loss is None or log_loss > 0.75:
        blockers.append("walk_forward_log_loss_too_high")
    clv_count = validation_metrics.get("clv_5m_count", 0)
    if clv_count >= 5 and validation_metrics.get("average_clv_5m_cents", -999.0) < -0.50:
        blockers.append("walk_forward_clv_negative")
    if baseline_metrics.get("event_count", 0) >= 8:
        if validation_metrics.get("roi_pct", -999.0) < baseline_metrics.get("roi_pct", 0.0) - 3.0:
            blockers.append("walk_forward_roi_worse_than_current_strategy")
        baseline_brier = baseline_metrics.get("brier")
        if (
            brier is not None
            and baseline_brier is not None
            and brier > baseline_brier + 0.03
        ):
            blockers.append("walk_forward_brier_worse_than_current_strategy")
    return {
        "unit": float(unit),
        "passed": not blockers,
        "method": "chronological_independent_event_walk_forward",
        "training_event_count": min(len(selected), required_train),
        "required_training_events": required_train,
        "required_validation_events": required_validation,
        "all_selected": _performance(selected),
        "validation": validation_metrics,
        "rescue_validation": rescue_metrics,
        "current_strategy_validation": baseline_metrics,
        "blockers": list(dict.fromkeys(blockers)),
    }


def _demotion_reason(rows, segment, promoted_max_units):
    selected = _decision_rows(rows, segment, max(0.5, float(promoted_max_units)))[-20:]
    metrics = _performance(selected)
    if metrics.get("event_count", 0) < 12:
        return "", metrics
    if metrics.get("profit_units", 0.0) <= -3.0 or metrics.get("roi_pct", 0.0) <= -8.0:
        return "rolling_profit_deterioration", metrics
    if metrics.get("brier") is not None and metrics["brier"] > 0.32:
        return "rolling_calibration_deterioration", metrics
    if metrics.get("clv_5m_count", 0) >= 6 and metrics.get("average_clv_5m_cents", 0.0) < -1.5:
        return "rolling_clv_deterioration", metrics
    return "", metrics


def update_promotion_state(
    resolved_observations,
    previous_state=None,
    *,
    enabled=True,
    auto_promote=True,
    min_train_events=18,
    min_validation_events=12,
):
    """Recalculate segment gates and apply promotion/demotion hysteresis."""
    previous = previous_state if isinstance(previous_state, dict) else empty_state()
    if previous.get("config_hash") != CONFIG_HASH:
        previous = empty_state()
    previous_segments = previous.get("segments") or {}
    rows = [
        row for row in resolved_observations or []
        if ((row.get("qualification_v2") or {}).get("version") == QUALIFICATION_VERSION)
        and ((row.get("qualification_v2") or {}).get("config_hash") == CONFIG_HASH)
    ]
    observed_segments = {
        (row.get("qualification_v2") or {}).get("segment")
        for row in rows
        if (row.get("qualification_v2") or {}).get("segment")
    }
    segments = sorted(observed_segments | set(previous_segments))
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    new_segments = {}
    transitions = list(previous.get("transitions") or [])
    for segment in segments:
        prior = previous_segments.get(segment) or {}
        gates = {
            _unit_key(unit): _unit_gate(rows, segment, unit, min_train_events, min_validation_events)
            for unit in _unit_steps()
        }
        eligible_max = 0.0
        for unit in _unit_steps():
            if not gates[_unit_key(unit)]["passed"]:
                break
            eligible_max = unit
        prior_max = float(_number(prior.get("promoted_max_units"), 0) or 0)
        promoted_max = prior_max
        demotion_reason = ""
        rolling = {}
        if prior_max > 0:
            demotion_reason, rolling = _demotion_reason(rows, segment, prior_max)
            if demotion_reason:
                promoted_max = 0
            elif enabled and auto_promote and eligible_max > prior_max:
                promoted_max = eligible_max
        elif enabled and auto_promote and eligible_max > 0:
            promoted_max = eligible_max
        if promoted_max != prior_max:
            transitions.append({
                "at": now,
                "segment": segment,
                "from_units": prior_max,
                "to_units": promoted_max,
                "reason": demotion_reason or "walk_forward_promotion_passed",
            })
        new_segments[segment] = {
            "status": "promoted" if promoted_max > 0 else "shadow",
            "promoted_max_units": promoted_max,
            "eligible_max_units": eligible_max,
            "unit_gates": gates,
            "rolling_demotion_review": rolling,
            "demotion_reason": demotion_reason,
            "updated_at": now,
        }
    return {
        "version": STATE_VERSION,
        "qualification_version": QUALIFICATION_VERSION,
        "config_hash": CONFIG_HASH,
        "generated_at": now,
        "enabled": bool(enabled),
        "auto_promote": bool(auto_promote),
        "min_train_events": int(min_train_events),
        "min_validation_events": int(min_validation_events),
        "eligible_resolved_observations": len(rows),
        "segments": new_segments,
        "transitions": transitions[-200:],
    }


def summary(candidates, promotion_state=None):
    reviews = [candidate.get("qualification_v2") or {} for candidate in candidates or []]
    state_segments = (promotion_state or {}).get("segments") or {}
    segment_status = {}
    for key, value in state_segments.items():
        promoted = float(value.get("promoted_max_units") or 0)
        next_unit = next(
            (unit for unit in _unit_steps() if unit > promoted + 1e-9),
            _unit_steps()[-1],
        )
        next_gate = (value.get("unit_gates") or {}).get(_unit_key(next_unit)) or {}
        segment_status[key] = {
            "status": value.get("status") or "shadow",
            "promoted_max_units": promoted,
            "eligible_max_units": float(value.get("eligible_max_units") or 0),
            "next_unit": next_unit,
            "next_unit_passed": next_gate.get("passed"),
            "next_unit_training_event_count": next_gate.get("training_event_count"),
            "next_unit_validation_event_count": (next_gate.get("validation") or {}).get("event_count"),
            "next_unit_blockers": next_gate.get("blockers") or [],
        }
    return {
        "version": QUALIFICATION_VERSION,
        "config_hash": CONFIG_HASH,
        "mode": "shadow_with_guarded_auto_promotion",
        "candidate_count": len(reviews),
        "shadow_qualified_count": sum(review.get("qualifies") is True for review in reviews),
        "shadow_rescue_count": sum(review.get("rescue_candidate") is True for review in reviews),
        "execution_active_count": sum(review.get("execution_active") is True for review in reviews),
        "recommended_units": dict(Counter(_unit_key(_number(review.get("recommended_units"), 0) or 0) for review in reviews)),
        "hard_blockers": dict(Counter(reason for review in reviews for reason in review.get("hard_blockers") or [])),
        "promoted_segments": {
            key: float(value.get("promoted_max_units") or 0)
            for key, value in state_segments.items()
            if float(value.get("promoted_max_units") or 0) > 0
        },
        "tracked_segments": len(state_segments),
        "segments": segment_status,
        "promotion_state": {
            "eligible_resolved_observations": (promotion_state or {}).get("eligible_resolved_observations", 0),
            "min_train_events": (promotion_state or {}).get("min_train_events"),
            "min_validation_events": (promotion_state or {}).get("min_validation_events"),
            "auto_promote": (promotion_state or {}).get("auto_promote"),
        },
    }
