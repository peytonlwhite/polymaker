"""Versioned, evidence-gated qualification for 15-minute crypto contracts.

The policy is evaluated on every scan, but remains shadow-only until independent
settled markets pass locked profitability, calibration, and temporal checks.
It deliberately replaces only economic thresholds after promotion; operational
and portfolio safeguards remain in the live campaign path.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone


POLICY_VERSION = "dynamic-qualification-v1"


def _number(value, default=0.0):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else float(default)
    except (TypeError, ValueError):
        try:
            return float(default)
        except (TypeError, ValueError):
            return 0.0


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


def _setting(settings, key, default):
    return (settings or {}).get(key, default)


def _tier_key(units):
    return f"{float(units):g}"


def dynamic_policy_configuration(settings):
    """Return the complete locked policy and a stable evidence identity."""
    tiers = {
        "0.5": {
            "lane": "exceptional_low_edge",
            "minimum_expected_edge_cents": _number(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MIN_EDGE_CENTS", 1.0)
            ),
            "minimum_qualification_margin_cents": _number(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MIN_MARGIN_CENTS", 0.25)
            ),
            "minimum_edge_probability": _number(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MIN_EDGE_PROBABILITY", 85.0)
            ),
            "minimum_data_quality": _number(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MIN_DATA_QUALITY", 0.98)
            ),
            "minimum_aligned_sources": _integer(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MIN_ALIGNED_SOURCES", 2)
            ),
            "maximum_opposing_sources": 0,
            "maximum_spread_cents": _number(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MAX_SPREAD_CENTS", 2.0)
            ),
            "maximum_market_gap_pp": _number(
                _setting(settings, "CRYPTO_DYNAMIC_HALF_MAX_MARKET_GAP_PP", 4.0)
            ),
            "minimum_win_probability": 0.0,
            "minimum_win_probability_low": 0.0,
        },
        "1": {
            "lane": "normal",
            "minimum_expected_edge_cents": _number(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MIN_EDGE_CENTS", 2.0)
            ),
            "minimum_qualification_margin_cents": _number(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MIN_MARGIN_CENTS", 0.50)
            ),
            "minimum_edge_probability": _number(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MIN_EDGE_PROBABILITY", 88.0)
            ),
            "minimum_data_quality": _number(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MIN_DATA_QUALITY", 0.96)
            ),
            "minimum_aligned_sources": _integer(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MIN_ALIGNED_SOURCES", 2)
            ),
            "maximum_opposing_sources": 1,
            "maximum_spread_cents": _number(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MAX_SPREAD_CENTS", 3.0)
            ),
            "maximum_market_gap_pp": _number(
                _setting(settings, "CRYPTO_DYNAMIC_ONE_MAX_MARKET_GAP_PP", 8.0)
            ),
            "minimum_win_probability": 0.0,
            "minimum_win_probability_low": 0.0,
        },
        "2": {
            "lane": "strong",
            "minimum_expected_edge_cents": 3.0,
            "minimum_qualification_margin_cents": 1.0,
            "minimum_edge_probability": 92.0,
            "minimum_data_quality": 0.96,
            "minimum_aligned_sources": 2,
            "maximum_opposing_sources": 1,
            "maximum_spread_cents": 3.0,
            "maximum_market_gap_pp": 8.0,
            "minimum_win_probability": 62.0,
            "minimum_win_probability_low": 55.0,
        },
        "3": {
            "lane": "elite",
            "minimum_expected_edge_cents": 4.0,
            "minimum_qualification_margin_cents": 1.5,
            "minimum_edge_probability": 94.0,
            "minimum_data_quality": 0.97,
            "minimum_aligned_sources": 2,
            "maximum_opposing_sources": 0,
            "maximum_spread_cents": 2.5,
            "maximum_market_gap_pp": 6.0,
            "minimum_win_probability": 68.0,
            "minimum_win_probability_low": 61.0,
        },
        "4": {
            "lane": "elite",
            "minimum_expected_edge_cents": 5.0,
            "minimum_qualification_margin_cents": 2.0,
            "minimum_edge_probability": 96.0,
            "minimum_data_quality": 0.98,
            "minimum_aligned_sources": 3,
            "maximum_opposing_sources": 0,
            "maximum_spread_cents": 2.0,
            "maximum_market_gap_pp": 5.0,
            "minimum_win_probability": 74.0,
            "minimum_win_probability_low": 67.0,
        },
        "5": {
            "lane": "elite",
            "minimum_expected_edge_cents": 7.0,
            "minimum_qualification_margin_cents": 3.0,
            "minimum_edge_probability": 98.0,
            "minimum_data_quality": 0.99,
            "minimum_aligned_sources": 3,
            "maximum_opposing_sources": 0,
            "maximum_spread_cents": 2.0,
            "maximum_market_gap_pp": 4.0,
            "minimum_win_probability": 80.0,
            "minimum_win_probability_low": 73.0,
        },
    }
    configuration = {
        "version": POLICY_VERSION,
        "enabled": _boolean(
            _setting(settings, "CRYPTO_DYNAMIC_QUALIFICATION_ENABLED", True),
            True,
        ),
        "auto_promote": _boolean(
            _setting(settings, "CRYPTO_DYNAMIC_AUTO_PROMOTE_ENABLED", True),
            True,
        ),
        "minimum_contract_price_cents": _number(
            _setting(settings, "CRYPTO_15M_MIN_ENTRY_PRICE_CENTS", 40.0)
        ),
        "maximum_contract_price_cents": _number(
            _setting(settings, "CRYPTO_15M_MAX_ENTRY_PRICE_CENTS", 70.0)
        ),
        "minimum_minutes_remaining": _number(
            _setting(settings, "CRYPTO_15M_MIN_MINUTES_REMAINING", 2.0)
        ),
        "maximum_minutes_remaining": _number(
            _setting(settings, "CRYPTO_15M_MAX_MINUTES_REMAINING", 12.0)
        ),
        "hard_minimum_data_quality": _number(
            _setting(settings, "CRYPTO_DYNAMIC_HARD_MIN_DATA_QUALITY", 0.95)
        ),
        "maximum_kalshi_age_seconds": _number(
            _setting(settings, "CRYPTO_DYNAMIC_MAX_KALSHI_AGE_SECONDS", 2.0)
        ),
        "minimum_healthy_sources": _integer(
            _setting(settings, "CRYPTO_DYNAMIC_MIN_HEALTHY_SOURCES", 2)
        ),
        "maximum_severe_model_disagreement_pp": _number(
            _setting(settings, "CRYPTO_DYNAMIC_MAX_MODEL_DISAGREEMENT_PP", 15.0)
        ),
        "minimum_independent_markets": _integer(
            _setting(settings, "CRYPTO_DYNAMIC_MIN_SETTLED_MARKETS", 100)
        ),
        "recent_validation_markets": _integer(
            _setting(settings, "CRYPTO_DYNAMIC_RECENT_VALIDATION_MARKETS", 40)
        ),
        "maximum_calibration_gap": _number(
            _setting(settings, "CRYPTO_DYNAMIC_MAX_CALIBRATION_GAP", 0.05)
        ),
        "maximum_recent_calibration_gap": _number(
            _setting(settings, "CRYPTO_DYNAMIC_MAX_RECENT_CALIBRATION_GAP", 0.075)
        ),
        "tiers": tiers,
    }
    payload = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    configuration["policy_hash"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return configuration


def _flow_evidence(candidate, minimum_strength=0.05):
    side_sign = 1.0 if str(candidate.get("side") or "yes").lower() == "yes" else -1.0
    flow = candidate.get("flow_review") or {}
    scores = flow.get("source_scores") or {}
    aligned = 0
    opposing = 0
    for value in scores.values():
        normalized = side_sign * _number(value)
        if normalized >= minimum_strength:
            aligned += 1
        elif normalized <= -minimum_strength:
            opposing += 1
    direction = str(flow.get("direction") or "neutral").lower()
    composite_opposes = bool(
        direction in {"yes", "no"}
        and direction != str(candidate.get("side") or "yes").lower()
        and _number(flow.get("strength")) >= 0.10
    )
    return {
        "aligned_sources": aligned,
        "opposing_sources": opposing,
        "healthy_sources": _integer(flow.get("healthy_source_count"), len(scores)),
        "composite_direction": direction,
        "composite_strength": round(_number(flow.get("strength")), 6),
        "composite_opposes": composite_opposes,
        "source_scores": dict(scores),
    }


def dynamic_qualification_review(
    settings,
    candidate,
    execution_pricing,
    sizing_probability,
    configured_tiers,
    policy_state=None,
):
    """Evaluate the locked policy for every available unit tier."""
    config = dynamic_policy_configuration(settings)
    state = policy_state or {}
    state_matches = bool(state.get("policy_hash") == config["policy_hash"])
    qualified = bool(state_matches and state.get("qualified_for_activation"))
    live = bool(
        config["enabled"]
        and config["auto_promote"]
        and qualified
        and state.get("status") == "active"
    )
    quality = _number((candidate.get("data_quality") or {}).get("score"))
    kalshi = candidate.get("kalshi_microstructure") or {}
    probability = candidate.get("probability") or {}
    flow = _flow_evidence(candidate)
    entry = _number(candidate.get("entry_price"))
    minutes = _number(candidate.get("minutes_to_close"))
    kalshi_age = _number(
        kalshi.get("age_seconds", kalshi.get("book_age_seconds")),
        999.0,
    )
    settlement_source = (
        probability.get("settlement_source")
        or probability.get("settlement_price_source")
        or (candidate.get("model") or {}).get("settlement_source")
    )
    disagreement_guard = candidate.get("disagreement_guard") or {}
    model_disagreement = _number(probability.get("model_disagreement"))
    hard_vetoes = []
    if not config["enabled"]:
        hard_vetoes.append("dynamic_policy_disabled")
    if not (config["minimum_contract_price_cents"] <= entry <= config["maximum_contract_price_cents"]):
        hard_vetoes.append("contract_price_range")
    if not (config["minimum_minutes_remaining"] <= minutes <= config["maximum_minutes_remaining"]):
        hard_vetoes.append("entry_window")
    if not candidate.get("fee_schedule_exact"):
        hard_vetoes.append("fee_schedule_unverified")
    if not settlement_source:
        hard_vetoes.append("settlement_mapping_unavailable")
    if quality < config["hard_minimum_data_quality"]:
        hard_vetoes.append("data_quality_degraded")
    if kalshi.get("sequence_valid") is False:
        hard_vetoes.append("kalshi_sequence_invalid")
    if kalshi_age > config["maximum_kalshi_age_seconds"]:
        hard_vetoes.append("kalshi_quote_stale")
    if flow["healthy_sources"] < config["minimum_healthy_sources"]:
        hard_vetoes.append("directional_data_unavailable")
    if flow["opposing_sources"] >= 2 or flow["composite_opposes"]:
        hard_vetoes.append("directional_opposition")
    if disagreement_guard.get("enabled") and not disagreement_guard.get("ok"):
        hard_vetoes.append(
            disagreement_guard.get("reason") or "model_market_disagreement_unconfirmed"
        )
    if model_disagreement > config["maximum_severe_model_disagreement_pp"]:
        hard_vetoes.append("severe_model_disagreement")
    live_quote = candidate.get("live_quote_refresh") or {}
    if live_quote.get("enabled") and live_quote.get("ok") is False:
        hard_vetoes.append(live_quote.get("error") or "live_quote_reconfirmation_failed")
    if not (execution_pricing or {}).get("available"):
        hard_vetoes.append("executable_book_unavailable")
    hard_vetoes = list(dict.fromkeys(hard_vetoes))

    win_probability = _number(sizing_probability.get("win_probability"))
    conservative_probability = _number(
        sizing_probability.get("sizing_probability_low"),
        sizing_probability.get("win_probability_low"),
    )
    market_gap = _number(disagreement_guard.get("raw_market_gap"))
    spread = _number(kalshi.get("spread_yes_cents"), 999.0)
    tier_reviews = {}
    passed = []
    for units in sorted(float(value) for value in configured_tiers):
        key = _tier_key(units)
        requirement = config["tiers"].get(key)
        execution = ((execution_pricing or {}).get("tiers") or {}).get(key) or {}
        if not requirement:
            tier_reviews[key] = {
                "units": units,
                "eligible": False,
                "reason": "dynamic_tier_not_configured",
            }
            continue
        break_even = _number(execution.get("break_even_probability"), 999.0)
        expected_edge = _number(execution.get("expected_edge"), -999.0)
        edge_low = _number(execution.get("edge_low"), -999.0)
        edge_probability = _number(
            execution.get("probability_net_edge_positive"),
            -999.0,
        )
        margin = conservative_probability - break_even
        checks = {
            "hard_veto_clear": not hard_vetoes,
            "complete_executable_depth": bool(execution.get("complete_depth")),
            "expected_edge": expected_edge >= requirement["minimum_expected_edge_cents"],
            "qualification_margin": margin >= requirement["minimum_qualification_margin_cents"],
            "edge_probability": edge_probability >= requirement["minimum_edge_probability"],
            "data_quality": quality >= requirement["minimum_data_quality"],
            "aligned_sources": flow["aligned_sources"] >= requirement["minimum_aligned_sources"],
            "opposing_sources": flow["opposing_sources"] <= requirement["maximum_opposing_sources"],
            "spread": spread <= requirement["maximum_spread_cents"],
            "market_gap": market_gap <= requirement["maximum_market_gap_pp"],
            "win_probability": win_probability >= requirement["minimum_win_probability"],
            "win_probability_low": conservative_probability >= requirement["minimum_win_probability_low"],
        }
        eligible = all(checks.values())
        if eligible:
            passed.append(units)
        tier_reviews[key] = {
            "units": units,
            "lane": requirement["lane"],
            "eligible": eligible,
            "reason": "eligible" if eligible else next(
                (name for name, ok in checks.items() if not ok),
                "dynamic_quality_filter",
            ),
            "checks": checks,
            "observed": {
                "expected_edge_cents": round(expected_edge, 4),
                "edge_low_cents": round(edge_low, 4),
                "qualification_margin_cents": round(margin, 4),
                "probability_net_edge_positive": round(edge_probability, 4),
                "break_even_probability": round(break_even, 4),
                "conservative_probability": round(conservative_probability, 4),
                "win_probability": round(win_probability, 4),
                "data_quality": round(quality, 4),
                "spread_cents": round(spread, 4),
                "market_gap_pp": round(market_gap, 4),
                "aligned_sources": flow["aligned_sources"],
                "opposing_sources": flow["opposing_sources"],
            },
            "requirements": dict(requirement),
        }
    target_units = max(passed, default=0.0)
    selected = tier_reviews.get(_tier_key(target_units), {}) if target_units else {}
    return {
        "version": POLICY_VERSION,
        "policy_hash": config["policy_hash"],
        "enabled": config["enabled"],
        "status": "active" if live else "shadow" if config["enabled"] else "disabled",
        "affects_execution": live,
        "qualified_for_activation": qualified,
        "activation_reason": state.get("activation_reason") or "collecting_independent_markets",
        "shadow_eligible": bool(target_units > 0),
        "eligible": bool(live and target_units > 0),
        "target_units": target_units,
        "lane": selected.get("lane") or "",
        "reason": (
            "eligible"
            if live and target_units > 0
            else "shadow_eligible"
            if target_units > 0
            else hard_vetoes[0]
            if hard_vetoes
            else "dynamic_quality_filter"
        ),
        "hard_vetoes": hard_vetoes,
        "flow_evidence": flow,
        "passed_tiers": passed,
        "tiers": tier_reviews,
        "contract_price_range_cents": [
            config["minimum_contract_price_cents"],
            config["maximum_contract_price_cents"],
        ],
        "entry_window_minutes": [
            config["minimum_minutes_remaining"],
            config["maximum_minutes_remaining"],
        ],
        "validation": {
            "independent_markets": _integer(state.get("independent_markets")),
            "minimum_independent_markets": config["minimum_independent_markets"],
            "checks": state.get("activation_checks") or {},
        },
    }


def settled_policy_outcome(card, result, *, policy_name="dynamic"):
    """Create one after-fee, market-level outcome from a frozen candidate card."""
    result = str(result or "").lower()
    side = str(card.get("side") or "").lower()
    if result not in {"yes", "no"} or side not in {"yes", "no"}:
        return None
    if policy_name == "dynamic":
        review = card.get("dynamic_qualification") or {}
        units = _number(review.get("target_units"))
    else:
        review = card.get("unit_sizing") or {}
        units = _number(review.get("target_units"))
    key = _tier_key(units)
    tier = (((card.get("execution") or {}).get("unit_tiers") or {}).get("tiers") or {}).get(key) or {}
    contracts = _number(tier.get("contracts"))
    book_cost = _number(tier.get("book_cost_dollars"))
    fees = _number(tier.get("fee_dollars"))
    adverse = _number(tier.get("expected_adverse_selection_dollars"))
    cost = book_cost + fees + adverse
    if units <= 0 or contracts <= 0 or cost <= 0 or not tier.get("complete_depth"):
        return None
    won = side == result
    profit = contracts - cost if won else -cost
    pricing = card.get("pricing") or {}
    probability = _number(pricing.get("selected_side_probability")) / 100.0
    break_even = _number(tier.get("break_even_probability")) / 100.0
    return {
        "policy": policy_name,
        "policy_version": (card.get("dynamic_qualification") or {}).get("version"),
        "policy_hash": (card.get("dynamic_qualification") or {}).get("policy_hash"),
        "ticker": card.get("ticker"),
        "asset": card.get("asset"),
        "side": side,
        "result": result,
        "won": won,
        "scanned_at": card.get("scanned_at"),
        "settled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "units": units,
        "contracts": int(contracts),
        "total_cost_dollars": round(cost, 4),
        "profit": round(profit, 4),
        "roi": round(profit / cost, 6),
        "predicted_win_probability": round(probability, 6),
        "market_break_even_probability": round(break_even, 6),
        "brier": round((probability - (1.0 if won else 0.0)) ** 2, 8),
        "market_brier": round((break_even - (1.0 if won else 0.0)) ** 2, 8),
        "lane": review.get("lane") or review.get("quality_tier"),
    }


def _maximum_drawdown(returns):
    total = peak = drawdown = 0.0
    for value in returns:
        total += value
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    return drawdown


def policy_outcome_metrics(outcomes):
    rows = [row for row in outcomes or [] if row.get("ticker")]
    if not rows:
        return {
            "independent_markets": 0,
            "profit": 0.0,
            "cost": 0.0,
            "roi": 0.0,
            "roi_lower_90": None,
            "calibration_gap": None,
            "brier": None,
            "market_brier": None,
            "maximum_drawdown_return_units": 0.0,
        }
    cost = sum(_number(row.get("total_cost_dollars")) for row in rows)
    profit = sum(_number(row.get("profit")) for row in rows)
    returns = [_number(row.get("roi")) for row in rows]
    mean_return = statistics.fmean(returns)
    standard_error = (
        statistics.stdev(returns) / math.sqrt(len(returns))
        if len(returns) >= 2
        else float("inf")
    )
    probabilities = [_number(row.get("predicted_win_probability")) for row in rows]
    observed = [1.0 if row.get("won") else 0.0 for row in rows]
    return {
        "independent_markets": len({str(row.get("ticker")) for row in rows}),
        "profit": round(profit, 4),
        "cost": round(cost, 4),
        "roi": round(profit / cost, 6) if cost > 0 else 0.0,
        "mean_market_return": round(mean_return, 6),
        "roi_lower_90": (
            round(mean_return - 1.645 * standard_error, 6)
            if math.isfinite(standard_error)
            else None
        ),
        "win_rate": round(statistics.fmean(observed), 6),
        "mean_predicted_probability": round(statistics.fmean(probabilities), 6),
        "calibration_gap": round(
            abs(statistics.fmean(probabilities) - statistics.fmean(observed)),
            6,
        ),
        "brier": round(statistics.fmean(_number(row.get("brier")) for row in rows), 8),
        "market_brier": round(
            statistics.fmean(_number(row.get("market_brier")) for row in rows),
            8,
        ),
        "maximum_drawdown_return_units": round(_maximum_drawdown(returns), 6),
    }


def dynamic_policy_validation(settings, outcomes, current_outcomes=None):
    """Locked market-level activation test; repeated scans never increase N."""
    config = dynamic_policy_configuration(settings)
    matching = [
        row for row in outcomes or []
        if row.get("policy_hash") == config["policy_hash"]
    ]
    deduplicated = {}
    for row in matching:
        deduplicated.setdefault(str(row.get("ticker")), row)
    rows = list(deduplicated.values())
    metrics = policy_outcome_metrics(rows)
    recent_count = max(1, config["recent_validation_markets"])
    recent = policy_outcome_metrics(rows[-recent_count:])
    current = policy_outcome_metrics(current_outcomes or [])
    comparator_roi = max(
        0.0,
        _number(current.get("roi")) if current.get("independent_markets", 0) >= 20 else 0.0,
    )
    windows = []
    if len(rows) >= 3:
        for index in range(3):
            start = math.floor(index * len(rows) / 3)
            end = math.floor((index + 1) * len(rows) / 3)
            windows.append(policy_outcome_metrics(rows[start:end]))
    checks = {
        "minimum_independent_markets": metrics["independent_markets"] >= config["minimum_independent_markets"],
        "positive_after_fee_roi": _number(metrics.get("roi")) > 0,
        "positive_roi_lower_90": metrics.get("roi_lower_90") is not None and _number(metrics.get("roi_lower_90")) > comparator_roi,
        "recent_after_fee_roi": recent["independent_markets"] >= recent_count and _number(recent.get("roi")) > 0,
        "calibration_gap": metrics.get("calibration_gap") is not None and _number(metrics.get("calibration_gap")) <= config["maximum_calibration_gap"],
        "recent_calibration_gap": recent.get("calibration_gap") is not None and _number(recent.get("calibration_gap")) <= config["maximum_recent_calibration_gap"],
        "brier_beats_market": metrics.get("brier") is not None and _number(metrics.get("brier")) <= _number(metrics.get("market_brier")),
        "temporal_windows_positive": len(windows) == 3 and all(_number(row.get("roi")) > 0 for row in windows),
    }
    qualified = bool(config["enabled"] and all(checks.values()))
    return {
        "version": POLICY_VERSION,
        "policy_hash": config["policy_hash"],
        "qualified": qualified,
        "independent_markets": metrics["independent_markets"],
        "minimum_independent_markets": config["minimum_independent_markets"],
        "metrics": metrics,
        "recent_metrics": recent,
        "current_policy_reference": current,
        "current_policy_roi_comparator": round(comparator_roi, 6),
        "temporal_windows": windows,
        "checks": checks,
        "affects_execution": False,
    }
