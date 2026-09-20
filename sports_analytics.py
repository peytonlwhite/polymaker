"""Decision-time analytics for the sports strategy.

Snapshots are deliberately compact and immutable.  Settlement-time values must
not overwrite them, which prevents resolved 0/100 prices from contaminating CLV
and calibration analysis.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

from sports_bet_intelligence import compact_intelligence
from sports_qualification_v2 import compact_qualification
from sports_audit_support import AUDIT_REVISION, binary_outcome, integrity_valid, parse_timestamp, strategy_lane


ANALYTICS_VERSION = "decision-analytics-v10-source-time"


def _number(value, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def compact_candidate_snapshot(candidate, *, scan_id, scan_reason, generated_at=None):
    pricing = candidate.get("pricing_v2") or {}
    consensus = pricing.get("consensus") or {}
    probability_sizing = (
        candidate.get("probability_sizing")
        or (candidate.get("sports_units") or {}).get("probability_sizing")
        or (candidate.get("sports_game_odds_units") or {}).get("probability_sizing")
        or {}
    )
    return {
        "version": ANALYTICS_VERSION,
        "audit_revision": AUDIT_REVISION,
        "source": candidate.get("source") or "edge_scanner",
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "scan_id": scan_id,
        "scan_reason": scan_reason,
        "event_id": candidate.get("event_id"),
        "game_key": candidate.get("game_key"),
        "kalshi_ticker": candidate.get("kalshi_ticker"),
        "sport_key": candidate.get("sport_key"),
        "market_type": candidate.get("market_type"),
        "selected_team": candidate.get("selected_team"),
        "order_side": candidate.get("order_side"),
        "market_line": candidate.get("market_line"),
        "game_started": bool(candidate.get("game_started")),
        "timing_bucket": (
            candidate.get("bet_timing_bucket")
            or candidate.get("timing_bucket")
            or pricing.get("timing_bucket")
        ),
        "minutes_since_start": candidate.get("minutes_since_start"),
        "live_score_summary": candidate.get("live_score_summary"),
        "live_score_context": candidate.get("live_score_context"),
        "odds_source": candidate.get("odds_source"),
        "odds_fetched_at": candidate.get("odds_fetched_at"),
        "odds_cache_age_minutes": candidate.get("odds_cache_age_minutes"),
        "latest_bookmaker_update_age_minutes": candidate.get("latest_bookmaker_update_age_minutes"),
        "live_odds_age_minutes": candidate.get("live_odds_age_minutes"),
        "entry_bid": pricing.get("bid_cents"),
        "entry_ask": pricing.get("ask_cents") if pricing.get("ask_cents") is not None else candidate.get("entry_price"),
        "entry_spread": pricing.get("spread_cents") if pricing.get("spread_cents") is not None else candidate.get("kalshi_spread"),
        "book_probability": pricing.get("book_probability"),
        "kalshi_mid_probability": pricing.get("kalshi_mid_probability"),
        "fair_probability": pricing.get("fair_probability") if pricing.get("fair_probability") is not None else candidate.get("model_prob"),
        "independent_outcome_probability": pricing.get("independent_outcome_probability"),
        "kalshi_market_probability": pricing.get("kalshi_market_probability"),
        "ensemble_probability_uncalibrated": pricing.get("ensemble_probability_uncalibrated"),
        "calibrated_ensemble_probability": pricing.get("calibrated_ensemble_probability"),
        "probability_decomposition": pricing.get("probability_decomposition") or {},
        "outcome_probability_challengers": candidate.get("outcome_probability_challengers") or {},
        "external_consensus_shadow": candidate.get("external_consensus_shadow") or {},
        "tennis_derivative_shadow": candidate.get("tennis_derivative_shadow") or {},
        "equivalent_contract_review": candidate.get("equivalent_contract_review") or {},
        "prematch_anchor": candidate.get("prematch_anchor") or {},
        "probability_distribution": pricing.get("probability_distribution") or {},
        "posterior_p_edge_positive": pricing.get("posterior_p_edge_positive"),
        "settlement_time_bucket": pricing.get("settlement_time_bucket"),
        "minutes_to_settlement": pricing.get("minutes_to_settlement"),
        "probability_price_band": pricing.get("probability_price_band"),
        "lower_probability": pricing.get("lower_probability"),
        "lower_probability_90": pricing.get("lower_probability_90"),
        "uncertainty_pp": pricing.get("uncertainty_pp"),
        "calibrated_win_probability": probability_sizing.get("calibrated_win_probability"),
        "lower_win_probability_90": probability_sizing.get("lower_win_probability_90"),
        "upper_win_probability_90": probability_sizing.get("upper_win_probability_90"),
        "sizing_probability": probability_sizing.get("sizing_probability"),
        "probability_sizing": probability_sizing,
        "raw_edge_pp": pricing.get("raw_edge_pp") if pricing.get("raw_edge_pp") is not None else candidate.get("raw_edge"),
        "net_conservative_edge_pp": pricing.get("net_conservative_edge_pp") if pricing.get("net_conservative_edge_pp") is not None else candidate.get("edge"),
        "p_edge_positive": pricing.get("p_edge_positive"),
        "book_weight": pricing.get("effective_book_weight"),
        "calibration_source": pricing.get("calibration_source"),
        "walk_forward_validation": (pricing.get("calibration_diagnostics") or {}).get("walk_forward_validation") or {},
        "pricing_version": pricing.get("version"),
        "independent_book_families": consensus.get("independent_family_count"),
        "raw_book_count": consensus.get("raw_book_count"),
        "sharp_book_count": consensus.get("sharp_book_count"),
        "average_book_age_minutes": consensus.get("average_age_minutes"),
        "line_ladder_interpolated_family_count": consensus.get("line_ladder_interpolated_family_count"),
        "line_ladder_max_distance": consensus.get("line_ladder_max_distance"),
        "line_ladder_uncertainty_pp": consensus.get("line_ladder_uncertainty_pp"),
        "confidence_score": candidate.get("confidence_score"),
        "data_quality_score": candidate.get("confidence_score"),
        "pro_score": (candidate.get("pro_review") or {}).get("score"),
        "final_score": candidate.get("final_bet_score"),
        "final_raw_score": (candidate.get("final_score_review") or {}).get("raw_score"),
        "final_score_calibration": (candidate.get("final_score_review") or {}).get("calibration"),
        "decision_model": candidate.get("decision_model"),
        "sports_units": candidate.get("sports_units") or {},
        "unit_count": (candidate.get("sports_units") or {}).get("additional_units"),
        "unit_target_units": (candidate.get("sports_units") or {}).get("target_units"),
        "skip_reasons": list(dict.fromkeys(candidate.get("skip_reasons") or [])),
        "display_skip_reasons": list(dict.fromkeys(candidate.get("display_skip_reasons") or [])),
        "skip_reason_details": candidate.get("skip_reason_details") or {},
        "would_execute": not bool(candidate.get("skip_reasons")),
        "ai_veto": bool((candidate.get("ai_risk") or {}).get("veto")),
        "game_state_features": candidate.get("game_state_features") or {},
        "bet_intelligence": compact_intelligence(candidate),
        "qualification_v2": compact_qualification(candidate),
        "stream_snapshot": candidate.get("stream_snapshot") or {},
        "pricing_update_token": consensus.get("update_token"),
        "edge_confirmation": candidate.get("edge_confirmation") or {},
        "strategy_version": candidate.get("strategy_version"),
        "strategy_config_hash": candidate.get("strategy_config_hash"),
        "strategy_build_id": candidate.get("strategy_build_id"),
        "strategy_process_started_at": candidate.get("strategy_process_started_at"),
    }


def _prediction(row, source):
    if source == "model":
        pricing = row.get("pricing_v2") or {}
        value = pricing.get("fair_probability", row.get("model_prob"))
    elif source == "calibrated":
        pricing = row.get("pricing_v2") or {}
        probability_sizing = (
            row.get("probability_sizing")
            or (row.get("sports_units") or {}).get("probability_sizing")
            or (row.get("sports_game_odds_units") or {}).get("probability_sizing")
            or {}
        )
        value = pricing.get(
            "calibrated_ensemble_probability",
            probability_sizing.get("calibrated_win_probability"),
        )
    elif source == "independent":
        pricing = row.get("pricing_v2") or {}
        value = pricing.get("independent_outcome_probability", pricing.get("book_probability"))
    elif source == "posterior":
        pricing = row.get("pricing_v2") or {}
        value = (pricing.get("probability_distribution") or {}).get("median_probability")
    elif source == "book":
        value = (row.get("pricing_v2") or {}).get("book_probability")
    elif source == "kalshi":
        pricing = row.get("pricing_v2") or {}
        value = pricing.get("kalshi_mid_probability")
        if value is None:
            bid = _number(row.get("entry_bid"))
            ask = _number(row.get("entry_price"))
            value = (bid + ask) / 2.0 if bid is not None and ask is not None else ask
    else:
        value = None
    value = _number(value)
    return None if value is None else max(0.001, min(0.999, value / 100.0))


def calibration_metrics(rows, source="model"):
    samples = []
    for row in rows or []:
        result = str(row.get("result") or "").upper()
        probability = _prediction(row, source)
        outcome = binary_outcome(row)
        if outcome is None or probability is None:
            continue
        samples.append((probability, outcome))
    if not samples:
        return {"source": source, "count": 0}
    brier = sum((probability - outcome) ** 2 for probability, outcome in samples) / len(samples)
    log_loss = -sum(
        outcome * math.log(probability) + (1.0 - outcome) * math.log(1.0 - probability)
        for probability, outcome in samples
    ) / len(samples)
    bins = defaultdict(list)
    for probability, outcome in samples:
        bins[min(9, int(probability * 10))].append((probability, outcome))
    calibration = []
    ece = 0.0
    for index, bucket in sorted(bins.items()):
        predicted = sum(row[0] for row in bucket) / len(bucket)
        actual = sum(row[1] for row in bucket) / len(bucket)
        ece += len(bucket) / len(samples) * abs(predicted - actual)
        calibration.append(
            {
                "bin": f"{index * 10}-{index * 10 + 9}",
                "count": len(bucket),
                "predicted_pct": round(predicted * 100.0, 2),
                "actual_pct": round(actual * 100.0, 2),
            }
        )
    return {
        "source": source,
        "count": len(samples),
        "average_probability_pct": round(sum(row[0] for row in samples) / len(samples) * 100.0, 3),
        "actual_win_rate_pct": round(sum(row[1] for row in samples) / len(samples) * 100.0, 3),
        "brier": round(brier, 6),
        "log_loss": round(log_loss, 6),
        "expected_calibration_error": round(ece, 6),
        "bins": calibration,
    }


def score_outcome_metrics(rows, field):
    samples = []
    for row in rows or []:
        result = str(row.get("result") or "").upper()
        if result not in {"WIN", "LOSS"}:
            continue
        if field == "pro_score":
            value = _number((row.get("pro_review") or {}).get("score", row.get(field)))
        elif field == "final_score":
            value = _number(row.get("final_bet_score", row.get(field)))
        else:
            value = _number(row.get(field))
        if value is None:
            continue
        samples.append((value, 1.0 if result == "WIN" else 0.0, _number(row.get("profit"), 0.0)))
    if not samples:
        return {"field": field, "count": 0}
    buckets = defaultdict(list)
    for value, outcome, profit in samples:
        lower = int(math.floor(value / 5.0) * 5)
        buckets[lower].append((value, outcome, profit))
    return {
        "field": field,
        "count": len(samples),
        "min": round(min(row[0] for row in samples), 2),
        "max": round(max(row[0] for row in samples), 2),
        "average": round(sum(row[0] for row in samples) / len(samples), 3),
        "buckets": [
            {
                "range": f"{lower}-{lower + 4.9:g}",
                "count": len(bucket),
                "average_score": round(sum(row[0] for row in bucket) / len(bucket), 3),
                "win_rate_pct": round(sum(row[1] for row in bucket) / len(bucket) * 100.0, 2),
                "profit": round(sum(row[2] for row in bucket), 2),
            }
            for lower, bucket in sorted(buckets.items())
        ],
    }


def fixed_horizon_clv_metrics(rows):
    horizons = defaultdict(list)
    for row in rows or []:
        for key, mark in (row.get("fixed_horizon_clv") or {}).items():
            if not isinstance(mark, dict) or not mark.get("on_time"):
                continue
            horizons[str(key)].append(mark)
    result = {}
    for key, marks in sorted(horizons.items()):
        kalshi_values = [
            value for mark in marks
            if (value := _number(mark.get("clv_vs_entry_ask_cents"))) is not None
        ]
        book_values = [
            value for mark in marks
            if mark.get("book_identity_valid") is True
            and mark.get("book_time_valid") is True
            and (value := _number(mark.get("book_move_pp"))) is not None
        ]
        pending_book_identity = Counter(
            str(mark.get("book_identity_reason") or "identity_pending")
            for mark in marks
            if mark.get("book_identity_pending") is True
        )
        invalid_book_identity = Counter(
            str(mark.get("book_identity_reason") or "legacy_unverified_side_line")
            for mark in marks
            if mark.get("book_identity_valid") is not True
            and mark.get("book_identity_pending") is not True
        )
        result[key] = {
            "count": len(marks),
            "kalshi_clv_count": len(kalshi_values),
            "average_kalshi_clv_cents": (
                round(sum(kalshi_values) / len(kalshi_values), 3)
                if kalshi_values else None
            ),
            "book_move_count": len(book_values),
            "book_identity_valid_count": sum(
                mark.get("book_identity_valid") is True for mark in marks
            ),
            "book_time_valid_count": sum(mark.get("book_time_valid") is True for mark in marks),
            "book_time_unverified_count": sum(mark.get("book_identity_valid") is True and mark.get("book_time_valid") is not True for mark in marks),
            "book_identity_invalid_count": sum(invalid_book_identity.values()),
            "book_identity_invalid_reasons": dict(invalid_book_identity),
            "book_identity_pending_count": sum(pending_book_identity.values()),
            "book_identity_pending_reasons": dict(pending_book_identity),
            "average_book_move_pp": (
                round(sum(book_values) / len(book_values), 3)
                if book_values else None
            ),
            # A live-game probability naturally changes with play.  This name is
            # intentionally explicit so it is not mistaken for pregame CLV.
            "average_forward_sportsbook_drift_pp": (
                round(sum(book_values) / len(book_values), 3)
                if book_values else None
            ),
        }
    return result


def event_grouped_calibration_metrics(rows, source="model"):
    """Calibration with each game contributing one unit of total weight."""
    samples = []
    group_counts = Counter(
        str(row.get("game_key") or row.get("event_id") or row.get("kalshi_ticker") or f"row-{index}")
        for index, row in enumerate(rows or [])
        if binary_outcome(row) is not None
        and _prediction(row, source) is not None
    )
    for index, row in enumerate(rows or []):
        result = str(row.get("result") or "").upper()
        probability = _prediction(row, source)
        outcome = binary_outcome(row)
        if outcome is None or probability is None:
            continue
        group = str(row.get("game_key") or row.get("event_id") or row.get("kalshi_ticker") or f"row-{index}")
        samples.append((probability, outcome, 1.0 / group_counts[group]))
    if not samples:
        return {"source": source, "count": 0, "effective_event_count": 0}
    total_weight = sum(weight for _probability, _outcome, weight in samples)
    brier = sum(weight * (probability - outcome) ** 2 for probability, outcome, weight in samples) / total_weight
    return {
        "source": source,
        "count": len(samples),
        "effective_event_count": len(group_counts),
        "brier": round(brier, 6),
    }


def walk_forward_holdout_metrics(rows, source="model", train_fraction=0.7):
    """Chronological holdout summary; games never cross the split boundary."""
    ordered = sorted(
        rows or [],
        key=lambda row: parse_timestamp(row.get("settled_at") or row.get("placed_at") or row.get("generated_at")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    games = []
    seen = set()
    for index, row in enumerate(ordered):
        group = str(row.get("game_key") or row.get("event_id") or row.get("kalshi_ticker") or f"row-{index}")
        if group not in seen:
            seen.add(group)
            games.append(group)
    if len(games) < 20:
        return {"source": source, "status": "insufficient_games", "effective_event_count": len(games)}
    split = max(10, min(len(games) - 5, int(len(games) * float(train_fraction))))
    training = set(games[:split])
    test_rows = []
    for index, row in enumerate(ordered):
        group = str(row.get("game_key") or row.get("event_id") or row.get("kalshi_ticker") or f"row-{index}")
        if group not in training:
            test_rows.append(row)
    return {
        "source": source,
        "status": "ok",
        "method": "chronological_stored_prediction_holdout_not_retraining",
        "training_event_count": len(training),
        "holdout": event_grouped_calibration_metrics(test_rows, source),
    }


def final_score_threshold_shadow(rows, threshold=90.0):
    settled = [row for row in rows or [] if str(row.get("result") or "").upper() in {"WIN", "LOSS"}]
    result = {}
    for label, predicate in (
        ("below_threshold", lambda value: value < threshold),
        ("at_or_above_threshold", lambda value: value >= threshold),
    ):
        selected = []
        for row in settled:
            score = _number(row.get("final_bet_score"))
            if score is not None and predicate(score):
                selected.append(row)
        stake = sum(_number(row.get("stake"), 0.0) for row in selected)
        profit = sum(_number(row.get("profit"), 0.0) for row in selected)
        result[label] = {
            "count": len(selected),
            "stake": round(stake, 2),
            "profit": round(profit, 2),
            "roi_pct": round(profit / stake * 100.0, 2) if stake > 0 else None,
        }
    return {"threshold": float(threshold), **result}


def pricing_analytics(rows, *, lane="autonomous"):
    settled = [
        row
        for row in rows or []
        if str(row.get("result") or "").upper() in {"WIN", "LOSS"}
        and isinstance(row.get("pricing_v2"), dict)
        and strategy_lane(row) == lane
        and integrity_valid(row)
    ]
    by_segment = defaultdict(list)
    for row in settled:
        key = "|".join(
            str(value or "unknown")
            for value in (
                row.get("sport_key"),
                row.get("market_type"),
                row.get("bet_timing_bucket"),
            )
        )
        by_segment[key].append(row)
    return {
        "version": ANALYTICS_VERSION,
        "strategy_lane": lane,
        "source_filter": "explicit_source_and_integrity",
        "tracked_settled": len(settled),
        "model": calibration_metrics(settled, "model"),
        "calibrated_model": calibration_metrics(settled, "calibrated"),
        "posterior_median": calibration_metrics(settled, "posterior"),
        "independent_outcome": calibration_metrics(settled, "independent"),
        "book_consensus": calibration_metrics(settled, "book"),
        "kalshi_mid": calibration_metrics(settled, "kalshi"),
        "event_grouped": {
            "model": event_grouped_calibration_metrics(settled, "model"),
            "calibrated_model": event_grouped_calibration_metrics(settled, "calibrated"),
            "posterior_median": event_grouped_calibration_metrics(settled, "posterior"),
            "independent_outcome": event_grouped_calibration_metrics(settled, "independent"),
            "book_consensus": event_grouped_calibration_metrics(settled, "book"),
            "kalshi_mid": event_grouped_calibration_metrics(settled, "kalshi"),
        },
        "walk_forward_holdout": {
            "model": walk_forward_holdout_metrics(settled, "model"),
            "calibrated_model": walk_forward_holdout_metrics(settled, "calibrated"),
            "posterior_median": walk_forward_holdout_metrics(settled, "posterior"),
            "independent_outcome": walk_forward_holdout_metrics(settled, "independent"),
            "book_consensus": walk_forward_holdout_metrics(settled, "book"),
            "kalshi_mid": walk_forward_holdout_metrics(settled, "kalshi"),
        },
        "final_score_90_shadow": final_score_threshold_shadow(settled, 90.0),
        "quality_scores": {
            "confidence": score_outcome_metrics(settled, "confidence_score"),
            "pro": score_outcome_metrics(settled, "pro_score"),
            "final": score_outcome_metrics(settled, "final_score"),
        },
        "fixed_horizon_clv": fixed_horizon_clv_metrics(settled),
        "segments": {
            key: {
                "model": calibration_metrics(segment_rows, "model"),
                "calibrated_model": calibration_metrics(segment_rows, "calibrated"),
                "posterior_median": calibration_metrics(segment_rows, "posterior"),
                "independent_outcome": calibration_metrics(segment_rows, "independent"),
                "kalshi_mid": calibration_metrics(segment_rows, "kalshi"),
            }
            for key, segment_rows in sorted(by_segment.items())
            if len(segment_rows) >= 10
        },
    }
