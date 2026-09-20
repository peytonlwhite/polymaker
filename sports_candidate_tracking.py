"""Incremental outcome tracking for placed and rejected sports candidates."""

from __future__ import annotations

from datetime import datetime, timezone

from kalshi_common import normalize_text
from sports_qualification_v2 import compact_qualification


TRACKING_VERSION = "candidate-outcomes-v5"
ADAPTIVE_SKIP_REASONS = {"adaptive_strategy_shadow"}
OVERSEAS_ROLLOUT_SKIP_REASONS = {
    "overseas_moneyline_shadow_validation",
    "overseas_totals_shadow_only",
    "overseas_live_state_unavailable",
    "cricket_shadow_only",
    "test_cricket_disabled",
    "table_tennis_disabled_no_consensus",
}
LARGE_OBSERVATION_FIELDS = {
    "probability_sizing",
    "outcome_probability_challengers",
    "game_state_shadow",
    "bet_intelligence",
}


def empty_registry():
    return {
        "version": TRACKING_VERSION,
        "pending": {},
        "resolved_observations": [],
        "resolved_tickers": {},
    }


def compact_registry(registry):
    """Remove decision-card copies that are not consumed by outcome models."""
    if not isinstance(registry, dict):
        return 0
    changed = 0
    rows = list(registry.get("resolved_observations") or [])
    for pending in (registry.get("pending") or {}).values():
        rows.extend(pending.get("observations") or [])
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in LARGE_OBSERVATION_FIELDS:
            if field in row:
                row.pop(field, None)
                changed += 1
        pricing = row.get("pricing_v2") or {}
        for field in ("probability_decomposition", "probability_distribution"):
            if field in pricing:
                pricing.pop(field, None)
                changed += 1
    if changed:
        registry["compacted_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        registry["compaction_version"] = "outcome-fields-v1"
    return changed


def _candidate_key(candidate):
    ticker = str(candidate.get("kalshi_ticker") or "").strip()
    side = "no" if normalize_text(candidate.get("order_side")) == "no" else "yes"
    return f"{ticker}|{side}" if ticker else ""


def _observation_key(candidate):
    pricing = candidate.get("pricing_v2") or {}
    consensus = pricing.get("consensus") or {}
    qualification_v2 = candidate.get("qualification_v2") or {}
    return "|".join(
        (
            str(consensus.get("update_token") or candidate.get("odds_fetched_at") or ""),
            str(pricing.get("ask_cents") if pricing.get("ask_cents") is not None else candidate.get("entry_price")),
            str(candidate.get("final_bet_score") or ""),
            str(qualification_v2.get("config_hash") or ""),
        )
    )


def compact_calibration_observation(candidate, generated_at=None):
    pricing = candidate.get("pricing_v2") or {}
    skip_reasons = list(dict.fromkeys(candidate.get("skip_reasons") or []))
    non_adaptive_skip_reasons = [
        reason for reason in skip_reasons if reason not in ADAPTIVE_SKIP_REASONS
    ]
    non_overseas_rollout_skip_reasons = [
        reason for reason in skip_reasons
        if reason not in OVERSEAS_ROLLOUT_SKIP_REASONS
    ]
    adaptive = candidate.get("adaptive_strategy") or {}
    return {
        "generated_at": generated_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "candidate_observation",
        "candidate_observation": True,
        "event_id": candidate.get("event_id"),
        "game_key": candidate.get("game_key"),
        "kalshi_ticker": candidate.get("kalshi_ticker"),
        "order_side": candidate.get("order_side"),
        "sport_key": candidate.get("sport_key"),
        "market_type": candidate.get("market_type"),
        "market_line": candidate.get("market_line"),
        "total_side": candidate.get("total_side"),
        "selected_team": candidate.get("selected_team"),
        "home_team": candidate.get("home_team"),
        "away_team": candidate.get("away_team"),
        "commence_time": candidate.get("commence_time"),
        "market_close_time": candidate.get("market_close_time"),
        "bet_timing_bucket": candidate.get("bet_timing_bucket") or pricing.get("timing_bucket"),
        "settlement_time_bucket": pricing.get("settlement_time_bucket"),
        "minutes_to_settlement": pricing.get("minutes_to_settlement"),
        "probability_price_band": pricing.get("probability_price_band"),
        "game_started": bool(candidate.get("game_started")),
        "entry_price": (
            pricing.get("ask_cents")
            if pricing.get("ask_cents") is not None
            else candidate.get("entry_price")
        ),
        "edge": candidate.get("edge"),
        "would_execute": not bool(skip_reasons),
        "would_execute_without_adaptive": not bool(non_adaptive_skip_reasons),
        "would_execute_without_overseas_rollout": not bool(
            non_overseas_rollout_skip_reasons
        ),
        "skip_reasons": skip_reasons,
        "adaptive_state_at_decision": adaptive.get("state") or "active",
        "pricing_update_token": (pricing.get("consensus") or {}).get("update_token"),
        "pricing_v2": {
            "version": pricing.get("version"),
            "book_probability": pricing.get("book_probability"),
            "kalshi_mid_probability": pricing.get("kalshi_mid_probability"),
            "fair_probability": pricing.get("fair_probability"),
            "independent_outcome_probability": pricing.get("independent_outcome_probability"),
            "kalshi_market_probability": pricing.get("kalshi_market_probability"),
            "ensemble_probability_uncalibrated": pricing.get("ensemble_probability_uncalibrated"),
            "calibrated_ensemble_probability": pricing.get("calibrated_ensemble_probability"),
            "empirical_uncertainty_pp": pricing.get("empirical_uncertainty_pp"),
            "p_edge_positive": pricing.get("p_edge_positive"),
            "posterior_p_edge_positive": pricing.get("posterior_p_edge_positive"),
            "calibration_source": pricing.get("calibration_source"),
            "settlement_time_bucket": pricing.get("settlement_time_bucket"),
            "probability_price_band": pricing.get("probability_price_band"),
            "line_ladder": {
                "interpolated_family_count": (pricing.get("consensus") or {}).get("line_ladder_interpolated_family_count"),
                "max_distance": (pricing.get("consensus") or {}).get("line_ladder_max_distance"),
                "uncertainty_pp": (pricing.get("consensus") or {}).get("line_ladder_uncertainty_pp"),
            },
        },
        "confidence_score": candidate.get("confidence_score"),
        "data_quality_score": candidate.get("data_quality_score"),
        "pro_score": (candidate.get("pro_review") or {}).get("score"),
        "p_edge_positive": pricing.get("p_edge_positive"),
        "posterior_p_edge_positive": pricing.get("posterior_p_edge_positive"),
        "final_bet_score": candidate.get("final_bet_score"),
        "qualification_v2": compact_qualification(candidate),
        "book_count": candidate.get("book_count"),
        "independent_book_family_count": candidate.get("independent_book_family_count"),
        "sharp_book_count": candidate.get("sharp_book_count"),
        "kalshi_spread": candidate.get("kalshi_spread"),
        "kalshi_volume": candidate.get("kalshi_volume"),
        "kalshi_liquidity": candidate.get("kalshi_liquidity"),
        "strategy_version": candidate.get("strategy_version"),
        "strategy_config_hash": candidate.get("strategy_config_hash"),
        "strategy_build_id": candidate.get("strategy_build_id"),
        "strategy_process_started_at": candidate.get("strategy_process_started_at"),
    }


def register_candidates(registry, candidates, *, max_observations_per_candidate=40):
    registry = registry if isinstance(registry, dict) else empty_registry()
    registry["version"] = TRACKING_VERSION
    registry.setdefault("pending", {})
    changed = 0
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for candidate in candidates or []:
        pricing = candidate.get("pricing_v2") or {}
        if not pricing.get("ok"):
            continue
        key = _candidate_key(candidate)
        if not key:
            continue
        row = registry["pending"].setdefault(
            key,
            {
                "ticker": candidate.get("kalshi_ticker"),
                "order_side": candidate.get("order_side") or "yes",
                "first_seen": now,
                "last_seen": now,
                "observation_keys": [],
                "observations": [],
            },
        )
        row["last_seen"] = now
        observation_key = _observation_key(candidate)
        if observation_key in row.get("observation_keys", []):
            continue
        row.setdefault("observation_keys", []).append(observation_key)
        row.setdefault("observations", []).append(compact_calibration_observation(candidate, now))
        row["observation_keys"] = row["observation_keys"][-max_observations_per_candidate:]
        row["observations"] = row["observations"][-max_observations_per_candidate:]
        changed += 1
    registry["generated_at"] = now
    return registry, changed


def _terminal_result(market):
    result = normalize_text(market.get("result"))
    if result in {"yes", "no"}:
        return result
    return ""


def _parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hypothetical_profit_units(observation, candidate_result):
    price = _number(observation.get("entry_price"))
    if price is None or not 0 < price < 100:
        return None
    probability = price / 100.0
    fee_per_stake_unit = 0.07 * (1.0 - probability)
    if candidate_result == "WIN":
        value = (1.0 - probability) / probability - fee_per_stake_unit
    else:
        value = -1.0 - fee_per_stake_unit
    return round(value, 6)


def _forward_clv(observations, index, horizon_minutes=5):
    current = observations[index]
    current_at = _parse_time(current.get("generated_at"))
    current_price = _number(current.get("entry_price"))
    if current_at is None or current_price is None:
        return None
    target_seconds = float(horizon_minutes) * 60.0
    eligible = []
    for later in observations[index + 1:]:
        later_at = _parse_time(later.get("generated_at"))
        later_price = _number(later.get("entry_price"))
        if later_at is None or later_price is None:
            continue
        elapsed = (later_at - current_at).total_seconds()
        if elapsed >= target_seconds:
            eligible.append((elapsed, later_price))
    if not eligible:
        return None
    _elapsed, later_price = min(eligible, key=lambda row: row[0])
    return round(later_price - current_price, 3)


def resolve_candidates(registry, markets_by_ticker, *, max_resolved_observations=25000):
    registry = registry if isinstance(registry, dict) else empty_registry()
    registry["version"] = TRACKING_VERSION
    pending = registry.setdefault("pending", {})
    resolved = registry.setdefault("resolved_observations", [])
    resolved_tickers = registry.setdefault("resolved_tickers", {})
    resolved_count = 0
    for key, row in list(pending.items()):
        ticker = str(row.get("ticker") or "")
        market = (markets_by_ticker or {}).get(ticker) or {}
        market_result = _terminal_result(market)
        if not market_result:
            continue
        order_side = "no" if normalize_text(row.get("order_side")) == "no" else "yes"
        candidate_result = "WIN" if order_side == market_result else "LOSS"
        settled_at = datetime.now().astimezone().isoformat(timespec="seconds")
        observations = sorted(
            row.get("observations") or [],
            key=lambda observation: _parse_time(observation.get("generated_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
        )
        for index, observation in enumerate(observations):
            resolved.append({
                **observation,
                "result": candidate_result,
                "settled_at": settled_at,
                "hypothetical_profit_units": _hypothetical_profit_units(
                    observation,
                    candidate_result,
                ),
                "shadow_clv_5m_cents": _forward_clv(observations, index, 5),
            })
            resolved_count += 1
        resolved_tickers[key] = {
            "market_result": market_result,
            "candidate_result": candidate_result,
            "settled_at": settled_at,
            "observation_count": len(observations),
        }
        del pending[key]
    registry["resolved_observations"] = resolved[-max_resolved_observations:]
    registry["generated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    return registry, resolved_count
