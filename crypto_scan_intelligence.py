"""Durable, decision-complete intelligence records for crypto candidate scans.

The live strategy can use a compact set of proven inputs while this module
retains the richer evidence needed to improve calibration, execution pricing,
and unit sizing without treating repeated scans as independent markets.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from crypto_dynamic_qualification import (
    POLICY_VERSION as DYNAMIC_POLICY_VERSION,
    dynamic_policy_configuration,
    dynamic_policy_validation,
    settled_policy_outcome,
)


INTELLIGENCE_SCHEMA_VERSION = 1
RESEARCH_FEATURE_SCHEMA_VERSION = 1
CHECKPOINT_SECONDS = (5, 15, 30, 60)


def _finite(value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _round(value, digits=6):
    parsed = _finite(value)
    return round(parsed, digits) if parsed is not None else None


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic_write(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, target)


def _read_json(path, default):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return payload if isinstance(payload, dict) else default
    except (OSError, json.JSONDecodeError):
        return default


def _append_jsonl(path, rows):
    rows = list(rows or [])
    if not rows:
        return 0
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = [json.dumps(row, sort_keys=True, separators=(",", ":")) for row in rows]
    incoming = sum(len(row.encode("utf-8")) + 1 for row in encoded)
    try:
        from storage_maintenance import maybe_rotate_for_append

        maybe_rotate_for_append(target, incoming_bytes=incoming)
    except Exception:
        pass
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        for row in encoded:
            handle.write(row + "\n")
    return len(rows)


def _feature(result, availability, name, value, *, scale=1.0, low=None, high=None):
    parsed = _finite(value)
    availability[name] = parsed is not None
    if parsed is None:
        result[name] = 0.0
        return
    parsed *= float(scale)
    if low is not None:
        parsed = max(float(low), parsed)
    if high is not None:
        parsed = min(float(high), parsed)
    result[name] = round(parsed, 8)


def _path_value(source, seconds):
    return source.get(f"mid_return_{seconds}s_bps")


def research_feature_payload(candidate, btc_context=None, scanned_at=None):
    """Return research-only values plus explicit missing-value indicators."""
    candidate = candidate or {}
    probability = candidate.get("probability") or {}
    model = candidate.get("model") or {}
    underlying = candidate.get("microstructure") or {}
    coinbase = underlying.get("coinbase") or {}
    kraken = underlying.get("kraken") or {}
    kalshi = candidate.get("kalshi_microstructure") or {}
    quality = candidate.get("data_quality") or {}
    partial = model.get("settlement_partial_window") or {}
    values = {}
    available = {}

    core = {
        "minutes_to_close": candidate.get("minutes_to_close"),
        "target_distance_sigma": probability.get("target_distance_sigma"),
        "market_probability_yes": probability.get("market_implied_yes"),
        "calibrated_probability_yes": probability.get("p_yes", probability.get("prob")),
        "probability_low_yes": probability.get("p_low"),
        "probability_high_yes": probability.get("p_high"),
        "model_disagreement_pp": probability.get("model_disagreement"),
        "raw_market_gap_pp": probability.get("raw_market_gap"),
        "minute_vol_bps": (
            _finite(model.get("minute_vol")) * 10000.0
            if _finite(model.get("minute_vol")) is not None
            else None
        ),
        "trend_bias": model.get("trend_bias"),
        "fakeout_risk": model.get("fakeout_risk"),
        "basis_buffer_bps": probability.get("basis_buffer_bps"),
        "learned_basis_mean_bps": probability.get("learned_settlement_basis_mean_bps"),
        "learned_basis_std_bps": probability.get("learned_settlement_basis_std_bps"),
        "learned_basis_markets": probability.get("learned_settlement_basis_markets"),
        "settlement_drift_minutes": probability.get("settlement_drift_minutes"),
        "settlement_variance_minutes": probability.get("settlement_variance_minutes"),
        "settlement_window_seconds": probability.get("settlement_window_seconds"),
        "data_quality": quality.get("score"),
        "cross_exchange_dispersion_bps": underlying.get("cross_exchange_dispersion_bps"),
        "settlement_proxy_source_count": underlying.get("settlement_proxy_source_count"),
        "market_volume": candidate.get("volume"),
        "market_liquidity": candidate.get("liquidity"),
        "market_open_interest": candidate.get("open_interest"),
    }
    for name, value in core.items():
        _feature(values, available, name, value)

    for prefix, source in (("coinbase", coinbase), ("kraken", kraken)):
        for field in (
            "spread_bps",
            "book_imbalance",
            "top_imbalance",
            "depth_usd",
            "bid_depth_usd",
            "ask_depth_usd",
            "top1_depth_usd",
            "top3_depth_usd",
            "top5_depth_usd",
            "book_slope_bps",
            "book_convexity",
            "microprice_displacement_bps",
            "book_age_seconds",
            "stream_age_seconds",
            "event_latency_ms",
        ):
            _feature(values, available, f"{prefix}_{field}", source.get(field))
        for seconds in (5, 15, 30, 60, 180):
            _feature(
                values,
                available,
                f"{prefix}_return_{seconds}s_bps",
                _path_value(source, seconds),
            )
        for seconds in (10, 30, 60, 300):
            _feature(
                values,
                available,
                f"{prefix}_trade_flow_{seconds}s",
                source.get(f"trade_flow_{seconds}s"),
                low=-1.0,
                high=1.0,
            )
            _feature(
                values,
                available,
                f"{prefix}_trade_count_{seconds}s_log",
                math.log1p(max(0.0, _finite(source.get(f"trade_count_{seconds}s")) or 0.0))
                if source.get(f"trade_count_{seconds}s") is not None
                else None,
            )
            _feature(
                values,
                available,
                f"{prefix}_trade_notional_{seconds}s_log",
                math.log1p(max(0.0, _finite(source.get(f"trade_notional_{seconds}s")) or 0.0))
                if source.get(f"trade_notional_{seconds}s") is not None
                else None,
            )
        short_flow = _finite(source.get("trade_flow_10s"))
        long_flow = _finite(source.get("trade_flow_60s"))
        _feature(
            values,
            available,
            f"{prefix}_flow_acceleration_10v60",
            short_flow - long_flow if short_flow is not None and long_flow is not None else None,
            low=-2.0,
            high=2.0,
        )

    kalshi_fields = {
        "kalshi_book_imbalance": kalshi.get("book_imbalance"),
        "kalshi_top_imbalance": kalshi.get("top_imbalance"),
        "kalshi_yes_depth_contracts": kalshi.get("yes_depth_contracts"),
        "kalshi_no_depth_contracts": kalshi.get("no_depth_contracts"),
        "kalshi_top1_depth_contracts": kalshi.get("top1_depth_contracts"),
        "kalshi_top3_depth_contracts": kalshi.get("top3_depth_contracts"),
        "kalshi_top5_depth_contracts": kalshi.get("top5_depth_contracts"),
        "kalshi_spread_cents": kalshi.get("spread_yes_cents"),
        "kalshi_microprice_displacement_cents": kalshi.get("microprice_displacement_yes_cents"),
        "kalshi_book_slope_cents": kalshi.get("book_slope_cents"),
        "kalshi_book_convexity": kalshi.get("book_convexity"),
        "kalshi_quote_age_seconds": kalshi.get("age_seconds"),
        "kalshi_event_latency_ms": kalshi.get("event_latency_ms"),
        "kalshi_volume": kalshi.get("volume"),
        "kalshi_open_interest": kalshi.get("open_interest"),
    }
    for name, value in kalshi_fields.items():
        _feature(values, available, name, value)
    for seconds in (5, 15, 30, 60, 180):
        _feature(
            values,
            available,
            f"kalshi_probability_return_{seconds}s_pp",
            kalshi.get(f"yes_mid_change_{seconds}s_pp"),
        )
    for seconds in (10, 30, 60, 300):
        _feature(
            values,
            available,
            f"kalshi_trade_flow_{seconds}s",
            kalshi.get(f"trade_flow_{seconds}s"),
            low=-1.0,
            high=1.0,
        )
        _feature(
            values,
            available,
            f"kalshi_trade_count_{seconds}s_log",
            math.log1p(max(0.0, _finite(kalshi.get(f"trade_count_{seconds}s")) or 0.0))
            if kalshi.get(f"trade_count_{seconds}s") is not None
            else None,
        )

    partial_observations = _finite(partial.get("observations"))
    partial_sources = _finite(partial.get("source_count"))
    window_seconds = _finite(probability.get("settlement_window_seconds"))
    expected_observations = (
        window_seconds * max(1.0, partial_sources)
        if window_seconds is not None and partial_sources is not None
        else None
    )
    partial_fraction = (
        min(1.0, partial_observations / expected_observations)
        if partial_observations is not None and expected_observations
        else None
    )
    for name, value in {
        "partial_settlement_observations": partial_observations,
        "partial_settlement_source_count": partial_sources,
        "partial_settlement_fraction": partial_fraction,
        "partial_settlement_average": partial.get("average"),
    }.items():
        _feature(values, available, name, value)

    btc_context = btc_context or candidate.get("btc_context") or {}
    for seconds in (15, 30, 60, 180):
        asset_return = _finite(coinbase.get(f"mid_return_{seconds}s_bps"))
        btc_return = _finite(btc_context.get(f"mid_return_{seconds}s_bps"))
        _feature(
            values,
            available,
            f"btc_relative_return_{seconds}s_bps",
            asset_return - btc_return
            if asset_return is not None and btc_return is not None
            else None,
        )

    when = _parse_time(scanned_at) or datetime.now(timezone.utc)
    radians = 2.0 * math.pi * (when.hour * 60 + when.minute) / 1440.0
    values.update({
        "utc_time_sin": round(math.sin(radians), 8),
        "utc_time_cos": round(math.cos(radians), 8),
        "is_weekend": 1.0 if when.weekday() >= 5 else 0.0,
    })
    available.update({"utc_time_sin": True, "utc_time_cos": True, "is_weekend": True})

    groups = {
        "coinbase_book": bool(coinbase.get("mid") and coinbase.get("book_age_seconds") is not None),
        "coinbase_trades": bool((_finite(coinbase.get("trade_count_60s")) or 0) > 0),
        "kraken_book": bool(kraken.get("mid") and kraken.get("book_age_seconds") is not None),
        "kraken_trades": bool((_finite(kraken.get("trade_count_60s")) or 0) > 0),
        "kalshi_book": bool(kalshi.get("best_yes_entry_price_cents") and kalshi.get("best_no_entry_price_cents")),
        "kalshi_trades": bool((_finite(kalshi.get("trade_count_60s")) or 0) > 0),
        "kalshi_sequence_valid": bool(kalshi.get("sequence_valid", True)),
        "settlement_partial_window": partial_observations is not None,
    }
    for name, present in groups.items():
        values[f"source_present_{name}"] = 1.0 if present else 0.0
        available[f"source_present_{name}"] = True
    return {
        "schema_version": RESEARCH_FEATURE_SCHEMA_VERSION,
        "values": values,
        "available": available,
        "source_presence": groups,
        "available_feature_count": sum(1 for value in available.values() if value),
        "feature_count": len(values),
    }


def _selected_probability(candidate, key="p_yes"):
    probability = candidate.get("probability") or {}
    value = probability.get(key)
    if value is None and key == "p_yes":
        value = probability.get("prob", candidate.get("model_prob_yes"))
    value = _finite(value)
    if value is None:
        return None
    return value if str(candidate.get("side") or "yes").lower() == "yes" else 100.0 - value


def _driver_rows(candidate):
    side_sign = 1.0 if str(candidate.get("side") or "yes").lower() == "yes" else -1.0
    probability = candidate.get("probability") or {}
    model = candidate.get("model") or {}
    micro = candidate.get("microstructure") or {}
    coinbase = micro.get("coinbase") or {}
    kraken = micro.get("kraken") or {}
    kalshi = candidate.get("kalshi_microstructure") or {}
    rows = []

    def add(label, raw, scale, detail=""):
        value = _finite(raw)
        if value is None:
            return
        score = side_sign * value / max(1e-9, float(scale))
        rows.append({
            "label": label,
            "direction": "supports" if score > 0.02 else "opposes" if score < -0.02 else "neutral",
            "strength": round(min(5.0, abs(score)), 4),
            "raw": round(value, 6),
            "detail": detail,
        })

    add("Target distance", probability.get("target_distance_sigma"), 1.0, "standard deviations from strike")
    add("Trend bias", model.get("trend_bias"), 1.0)
    add("Coinbase 30s price path", coinbase.get("mid_return_30s_bps"), 10.0, "basis points")
    add("Kraken 30s price path", kraken.get("mid_return_30s_bps"), 10.0, "basis points")
    add("Coinbase 60s flow", coinbase.get("trade_flow_60s"), 0.25)
    add("Kraken 60s flow", kraken.get("trade_flow_60s"), 0.25)
    add("Kalshi trade flow", kalshi.get("trade_flow_60s"), 0.25)
    add("Kalshi quantity imbalance", kalshi.get("book_imbalance"), 0.25, "diagnostic until independently validated")
    model_yes = _finite(probability.get("adaptive_source_probability_yes"))
    market_yes = _finite(probability.get("market_implied_yes"))
    if model_yes is not None and market_yes is not None:
        add("Residual model versus market", model_yes - market_yes, 5.0, "percentage-point gap")
    rows.sort(key=lambda row: row["strength"], reverse=True)
    return {
        "supports": [row for row in rows if row["direction"] == "supports"][:4],
        "opposes": [row for row in rows if row["direction"] == "opposes"][:4],
        "neutral": [row for row in rows if row["direction"] == "neutral"][:2],
        "method": "directionally_normalized_diagnostic_not_causal_attribution",
    }


def _decision_change(candidate):
    reasons = list(candidate.get("skip_reasons") or [])
    units = candidate.get("crypto_units") or {}
    metrics = units.get("metrics") or {}
    directional = candidate.get("directional_confirmation") or {}
    disagreement = candidate.get("disagreement_guard") or {}
    dynamic = candidate.get("dynamic_qualification") or {}
    edge_low = _finite(candidate.get("edge_low"))
    messages = []
    if dynamic.get("shadow_eligible") and not dynamic.get("affects_execution"):
        messages.append(
            f"Dynamic policy qualifies {float(dynamic.get('target_units') or 0):g}u in shadow; it remains non-executing until its independent-market validation gate passes."
        )
    if not reasons:
        return ["Candidate cleared the recorded strategy checks."]
    if "conservative_edge_not_positive" in reasons and edge_low is not None:
        messages.append(
            f"Conservative edge needs to improve by at least {abs(min(0.0, edge_low)) + 0.01:.2f}c, usually through a lower ask or a tighter probability interval."
        )
    if "crypto_unit_win_probability_filter" in reasons:
        requirements = (units.get("requirements") or {}).get("1") or {}
        win = _finite(metrics.get("win_probability")) or 0.0
        low = _finite(metrics.get("conservative_sizing_probability_low")) or 0.0
        need_win = max(0.0, (_finite(requirements.get("minimum_win_probability")) or 0.0) - win)
        need_low = max(0.0, (_finite(requirements.get("minimum_win_probability_low")) or 0.0) - low)
        messages.append(
            f"The 1u probability tier needs +{need_win:.2f}pp central probability and +{need_low:.2f}pp on its conservative sizing bound."
        )
    if "crypto_unit_edge_filter" in reasons:
        requirements = (units.get("requirements") or {}).get("1") or {}
        expected = _finite(candidate.get("expected_edge")) or 0.0
        low = edge_low or 0.0
        certainty = _finite(candidate.get("probability_net_edge_positive")) or 0.0
        messages.append(
            "The 1u edge tier needs "
            f"{max(0.0, (_finite(requirements.get('minimum_edge')) or 0.0) - expected):.2f}c more expected edge, "
            f"{max(0.0, (_finite(requirements.get('minimum_edge_low')) or 0.0) - low):.2f}c more conservative edge, and "
            f"{max(0.0, (_finite(requirements.get('minimum_confidence')) or 0.0) - certainty):.2f}pp more edge certainty."
        )
    if "model_market_disagreement_unconfirmed" in reasons:
        gap = _finite(disagreement.get("raw_market_gap")) or 0.0
        maximum = _finite(disagreement.get("maximum_unconfirmed_gap")) or 0.0
        messages.append(
            f"Model-versus-market disagreement must fall by {max(0.0, gap - maximum):.2f}pp or receive the configured independent flow confirmation."
        )
    if "directional_flow_too_weak" in reasons:
        strength = _finite(directional.get("flow_strength")) or 0.0
        minimum = _finite(directional.get("minimum_flow_strength")) or 0.0
        messages.append(f"Aligned directional strength needs +{max(0.0, minimum - strength):.3f}.")
    if "directional_insufficient_consensus" in reasons:
        count = int(_finite(directional.get("selected_side_source_count")) or 0)
        minimum = int(_finite(directional.get("minimum_sources")) or 0)
        messages.append(f"Needs {max(0, minimum - count)} additional fresh independent source vote(s).")
    if "directional_opposition" in reasons:
        messages.append("Strong opposing flow is a hard veto; wait for the opposing sources to clear rather than paying a higher price.")
    if "directional_data_unavailable" in reasons:
        messages.append("Restore fresh source coverage; unavailable data must not be interpreted as neutral flow.")
    if "campaign_price_range" in reasons:
        messages.append("The executable ask must return to the configured campaign price band.")
    if "campaign_entry_window" in reasons:
        messages.append("The market must be inside the configured time-to-settlement window.")
    if "live_quote_slippage_limit" in reasons:
        messages.append("A second stable quote must reconfirm within the configured slippage allowance.")
    if "crypto_dynamic_qualification_filter" in reasons:
        half = (dynamic.get("tiers") or {}).get("0.5") or {}
        observed = half.get("observed") or {}
        requirements = half.get("requirements") or {}
        messages.append(
            "The dynamic 0.5u lane needs "
            f"{max(0.0, (_finite(requirements.get('minimum_expected_edge_cents')) or 0) - (_finite(observed.get('expected_edge_cents')) or 0)):.2f}c more expected edge and "
            f"{max(0.0, (_finite(requirements.get('minimum_qualification_margin_cents')) or 0) - (_finite(observed.get('qualification_margin_cents')) or 0)):.2f}c more conservative qualification margin."
        )
    return messages or ["See the recorded skip checks; no single scalar change is sufficient."]


def candidate_card(candidate, scan_id=None, scanned_at=None, btc_context=None):
    scanned_at = scanned_at or datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    scan_id = scan_id or uuid.uuid4().hex
    probability = candidate.get("probability") or {}
    learned = candidate.get("learned_15m_model") or {}
    quality = candidate.get("data_quality") or {}
    kalshi = candidate.get("kalshi_microstructure") or {}
    units = candidate.get("crypto_units") or {}
    fee = _finite(candidate.get("exact_fee_cents")) or 0.0
    slip = _finite(candidate.get("expected_slippage_cents")) or 0.0
    entry = _finite(candidate.get("entry_price")) or 0.0
    selected_probability = _selected_probability(candidate)
    selected_low = _finite(candidate.get("selected_side_probability_low"))
    selected_high = _finite(candidate.get("selected_side_probability_high"))
    card = {
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "scan_id": scan_id,
        "scanned_at": scanned_at,
        "asset": candidate.get("asset"),
        "ticker": candidate.get("ticker"),
        "event_ticker": candidate.get("event_ticker"),
        "series_ticker": candidate.get("series_ticker"),
        "side": candidate.get("side"),
        "close_time": candidate.get("close_time"),
        "minutes_to_close": _round(candidate.get("minutes_to_close"), 4),
        "strategy_version": candidate.get("strategy_version"),
        "strategy_config_hash": candidate.get("strategy_config_hash"),
        "pricing": {
            "entry_price_cents": _round(entry, 4),
            "selected_side_probability": _round(selected_probability, 4),
            "selected_side_probability_low": _round(selected_low, 4),
            "selected_side_probability_high": _round(selected_high, 4),
            "break_even_probability": _round(entry + fee + slip, 4),
            "exact_fee_cents": _round(fee, 4),
            "expected_slippage_cents": _round(slip, 4),
            "expected_edge_cents": _round(candidate.get("expected_edge", candidate.get("edge")), 4),
            "edge_low_cents": _round(candidate.get("edge_low"), 4),
            "probability_net_edge_positive": _round(candidate.get("probability_net_edge_positive", candidate.get("confidence")), 4),
            "fee_schedule_exact": bool(candidate.get("fee_schedule_exact")),
        },
        "probability": {
            "market_implied_yes": _round(probability.get("market_implied_yes"), 4),
            "heuristic_yes": _round(probability.get("heuristic_prob_before_ml", probability.get("pre_anchor_prob")), 4),
            "learned_yes": _round(learned.get("prob_yes"), 4),
            "residual_source_yes": _round(probability.get("adaptive_source_probability_yes"), 4),
            "final_yes": _round(probability.get("p_yes", probability.get("prob")), 4),
            "low_yes": _round(probability.get("p_low"), 4),
            "high_yes": _round(probability.get("p_high"), 4),
            "interval_level": _round(probability.get("interval_level"), 4),
            "interval_status": probability.get("interval_status"),
            "source": probability.get("adaptive_probability_source"),
            "model_weight": _round(probability.get("adaptive_model_weight"), 6),
            "residual_weight": _round(probability.get("adaptive_residual_weight"), 6),
            "uncertainty_components": probability.get("uncertainty_components") or {},
        },
        "settlement": {
            "source": (
                probability.get("settlement_source")
                or probability.get("settlement_price_source")
                or (candidate.get("model") or {}).get("settlement_source")
            ),
            "target_distance_sigma": _round(probability.get("target_distance_sigma"), 5),
            "effective_target": _round(
                probability.get(
                    "effective_target",
                    probability.get("effective_remaining_window_target"),
                ),
                8,
            ),
            "basis_buffer_bps": _round(probability.get("basis_buffer_bps"), 4),
            "basis_mean_bps": _round(probability.get("learned_settlement_basis_mean_bps"), 4),
            "basis_std_bps": _round(probability.get("learned_settlement_basis_std_bps"), 4),
            "partial_window": (candidate.get("model") or {}).get("settlement_partial_window") or {},
        },
        "execution": {
            "spread_yes_cents": _round(kalshi.get("spread_yes_cents"), 4),
            "microprice_yes_cents": _round(kalshi.get("microprice_yes_cents"), 4),
            "book_imbalance": _round(kalshi.get("book_imbalance"), 6),
            "top_imbalance": _round(kalshi.get("top_imbalance"), 6),
            "yes_depth_contracts": _round(kalshi.get("yes_depth_contracts"), 4),
            "no_depth_contracts": _round(kalshi.get("no_depth_contracts"), 4),
            "quote_age_seconds": _round(kalshi.get("age_seconds"), 4),
            "event_latency_ms": _round(kalshi.get("event_latency_ms"), 3),
            "sequence_valid": bool(kalshi.get("sequence_valid", True)),
            "unit_tiers": candidate.get("execution_tier_pricing") or {},
            "actual_execution": candidate.get("execution_quality") or {},
        },
        "data_quality": quality,
        "flow_review": candidate.get("flow_review") or {},
        "directional_confirmation": candidate.get("directional_confirmation") or {},
        "campaign_review": candidate.get("crypto_live_campaign") or {},
        "disagreement_guard": candidate.get("disagreement_guard") or {},
        "unit_sizing": units,
        "dynamic_qualification": candidate.get("dynamic_qualification") or {},
        "drivers": _driver_rows(candidate),
        "decision": candidate.get("decision"),
        "skip_reasons": list(candidate.get("skip_reasons") or []),
        "what_would_change_decision": _decision_change(candidate),
        "placed_bet_id": candidate.get("placed_bet_id"),
        "placed_stake": _round(candidate.get("placed_stake"), 4),
        "research_features": research_feature_payload(
            candidate,
            btc_context=btc_context,
            scanned_at=scanned_at,
        ),
        "data_lineage": {
            "market_quote_fetched_at": candidate.get("market_quote_fetched_at"),
            "underlying_fetched_at": (candidate.get("microstructure") or {}).get("fetched_at"),
            "coinbase_book_age_seconds": _round(((candidate.get("microstructure") or {}).get("coinbase") or {}).get("book_age_seconds"), 4),
            "kraken_book_age_seconds": _round(((candidate.get("microstructure") or {}).get("kraken") or {}).get("book_age_seconds"), 4),
            "kalshi_received_at": kalshi.get("received_at") or kalshi.get("orderbook_fetched_at"),
            "kalshi_exchange_ts_ms": kalshi.get("exchange_ts_ms"),
        },
    }
    return card


def _mark_from_candidate(candidate):
    probability = candidate.get("probability") or {}
    micro = candidate.get("microstructure") or {}
    return {
        "entry_price_cents": _round(candidate.get("entry_price"), 4),
        "market_probability_yes": _round(probability.get("market_implied_yes"), 4),
        "calibrated_probability_yes": _round(probability.get("p_yes", probability.get("prob")), 4),
        "settlement_proxy_spot": _round(micro.get("settlement_proxy_spot") or (candidate.get("model") or {}).get("spot"), 8),
        "edge_low_cents": _round(candidate.get("edge_low"), 4),
        "data_quality": _round((candidate.get("data_quality") or {}).get("score"), 4),
    }


def _counterfactual_outcomes(active, result):
    side_won = str(active.get("side") or "").lower() == str(result or "").lower()
    rows = []
    tiers = ((active.get("card") or {}).get("execution") or {}).get("unit_tiers") or {}
    tier_rows = tiers.get("tiers") if isinstance(tiers, dict) else None
    for key, tier in (tier_rows or {}).items():
        contracts = _finite(tier.get("contracts")) or 0.0
        book_cost = _finite(tier.get("book_cost_dollars")) or 0.0
        fees = _finite(tier.get("fee_dollars")) or 0.0
        slippage = _finite(tier.get("expected_adverse_selection_dollars")) or 0.0
        total_cost = book_cost + fees + slippage
        profit = contracts - total_cost if side_won else -total_cost
        rows.append({
            "units": _round(tier.get("units", key), 4),
            "contracts": int(contracts),
            "complete_depth": bool(tier.get("complete_depth")),
            "total_cost_dollars": round(total_cost, 4),
            "counterfactual_profit": round(profit, 4),
            "counterfactual_roi": round(profit / total_cost, 6) if total_cost > 0 else None,
        })
    return rows


_COUNTERFACTUAL_COLLECTION_VETOES = {
    "contract_price_range",
    "entry_window",
    "fee_schedule_unverified",
    "settlement_mapping_unavailable",
    "data_quality_degraded",
    "kalshi_sequence_invalid",
    "kalshi_quote_stale",
    "directional_data_unavailable",
    "live_quote_reconfirmation_failed",
    "executable_book_unavailable",
    "orderbook_best_price_mismatch",
}


def _counterfactual_collection_eligible(card):
    """Keep one clean market snapshot even when economic policy checks fail."""
    review = card.get("dynamic_qualification") or {}
    if not review or not review.get("policy_hash"):
        return False
    hard_vetoes = set(review.get("hard_vetoes") or [])
    if hard_vetoes & _COUNTERFACTUAL_COLLECTION_VETOES:
        return False
    return not any(str(reason).startswith("live_quote_") for reason in hard_vetoes)


def _counterfactual_market_outcome(active, result, settled_at):
    """Create one frozen, market-level observation for shadow research only."""
    card = active.get("card") or {}
    side = str(card.get("side") or "").lower()
    result = str(result or "").lower()
    if side not in {"yes", "no"} or result not in {"yes", "no"}:
        return None
    pricing = card.get("pricing") or {}
    review = card.get("dynamic_qualification") or {}
    predicted = (_finite(pricing.get("selected_side_probability")) or 0.0) / 100.0
    won = side == result
    return {
        "type": "dynamic_qualification_counterfactual_settlement",
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "policy_version": review.get("version"),
        "policy_hash": review.get("policy_hash"),
        "scan_id": active.get("scan_id"),
        "ticker": card.get("ticker"),
        "asset": card.get("asset"),
        "side": side,
        "result": result,
        "won": won,
        "scanned_at": card.get("scanned_at"),
        "settled_at": settled_at,
        "entry_price_cents": pricing.get("entry_price_cents"),
        "minutes_to_close": card.get("minutes_to_close"),
        "predicted_win_probability": round(predicted, 6),
        "brier": round((predicted - (1.0 if won else 0.0)) ** 2, 8),
        "shadow_policy_selected": bool(review.get("shadow_eligible")),
        "shadow_target_units": _round(review.get("target_units"), 4),
        "shadow_lane": review.get("lane"),
        "hard_vetoes": list(review.get("hard_vetoes") or []),
        "tier_outcomes": _counterfactual_outcomes(active, result),
        "affects_execution": False,
        "purpose": "independent_market_counterfactual_validation",
    }


def _counterfactual_validation_summary(outcomes, minimum_markets):
    deduplicated = {}
    for row in outcomes or []:
        ticker = str(row.get("ticker") or "")
        if ticker:
            deduplicated.setdefault(ticker, row)
    rows = list(deduplicated.values())
    tier_groups = {}
    for row in rows:
        for tier in row.get("tier_outcomes") or []:
            if not tier.get("complete_depth"):
                continue
            key = f"{float(tier.get('units') or 0):g}"
            group = tier_groups.setdefault(
                key,
                {"units": _finite(tier.get("units")) or 0.0, "markets": 0, "cost": 0.0, "profit": 0.0},
            )
            group["markets"] += 1
            group["cost"] += _finite(tier.get("total_cost_dollars")) or 0.0
            group["profit"] += _finite(tier.get("counterfactual_profit")) or 0.0
    tiers = {}
    for key, group in tier_groups.items():
        cost = group["cost"]
        tiers[key] = {
            "units": group["units"],
            "independent_markets": group["markets"],
            "total_cost_dollars": round(cost, 4),
            "profit": round(group["profit"], 4),
            "roi": round(group["profit"] / cost, 6) if cost > 0 else None,
        }
    probabilities = [_finite(row.get("predicted_win_probability")) for row in rows]
    probabilities = [value for value in probabilities if value is not None]
    observed = [1.0 if row.get("won") else 0.0 for row in rows]
    calibration_gap = None
    if probabilities and len(probabilities) == len(observed):
        calibration_gap = abs(sum(probabilities) / len(probabilities) - sum(observed) / len(observed))
    return {
        "status": "ready_for_policy_research" if len(rows) >= minimum_markets else "collecting",
        "observed_independent_markets": len(rows),
        "minimum_observed_markets": int(minimum_markets),
        "calibration_gap": round(calibration_gap, 6) if calibration_gap is not None else None,
        "counterfactual_tiers": tiers,
        "affects_execution": False,
        "activation_uses_policy_selected_markets_only": True,
    }


def record_scan_intelligence(
    candidates,
    ledger_file,
    state_file,
    *,
    settlements=None,
    btc_context=None,
    settings=None,
    now=None,
    max_active=10000,
):
    """Persist every candidate plus delayed price marks and settlement replay."""
    now = now or datetime.now(timezone.utc)
    scanned_at = now.isoformat(timespec="milliseconds")
    scan_id = f"{int(now.timestamp() * 1000)}-{uuid.uuid4().hex[:8]}"
    state = _read_json(
        state_file,
        {
            "schema_version": INTELLIGENCE_SCHEMA_VERSION,
            "active": {},
            "stats": {},
        },
    )
    active = state.setdefault("active", {})
    stats = state.setdefault("stats", {})
    dynamic_config = dynamic_policy_configuration(settings or {})
    dynamic_state = dict(state.get("dynamic_policy") or {})
    if dynamic_state.get("policy_hash") != dynamic_config.get("policy_hash"):
        prior_summary = {
            key: dynamic_state.get(key)
            for key in (
                "version",
                "policy_hash",
                "status",
                "independent_markets",
                "updated_at",
            )
            if dynamic_state.get(key) is not None
        }
        if prior_summary:
            history = list(state.get("dynamic_policy_history") or [])
            history.append({**prior_summary, "retired_at": scanned_at})
            state["dynamic_policy_history"] = history[-20:]
        dynamic_state = {
            "version": DYNAMIC_POLICY_VERSION,
            "policy_hash": dynamic_config.get("policy_hash"),
            "status": "shadow",
            "qualified_for_activation": False,
            "activation_reason": "collecting_independent_markets",
            "selected": {},
            "current_policy_selected": {},
            "counterfactual_selected": {},
            "outcomes": [],
            "current_policy_outcomes": [],
            "counterfactual_outcomes": [],
            "transitions": [],
        }
    dynamic_selected = dynamic_state.setdefault("selected", {})
    current_policy_selected = dynamic_state.setdefault("current_policy_selected", {})
    counterfactual_selected = dynamic_state.setdefault("counterfactual_selected", {})
    dynamic_outcomes = dynamic_state.setdefault("outcomes", [])
    current_policy_outcomes = dynamic_state.setdefault("current_policy_outcomes", [])
    counterfactual_outcomes = dynamic_state.setdefault("counterfactual_outcomes", [])
    current_by_ticker = {
        str(row.get("ticker") or ""): row
        for row in candidates or []
        if row.get("ticker")
    }
    events = []

    settlement_map = {
        str(row.get("ticker") or ""): row
        for row in settlements or []
        if row.get("ticker") and str(row.get("result") or "").lower() in {"yes", "no"}
    }
    remove = []
    for active_id, row in list(active.items()):
        captured = _parse_time(row.get("scanned_at"))
        if not captured:
            remove.append(active_id)
            continue
        age_seconds = max(0.0, (now - captured).total_seconds())
        ticker = str(row.get("ticker") or "")
        current = current_by_ticker.get(ticker)
        checkpoints = row.setdefault("checkpoints", {})
        if current:
            for target in CHECKPOINT_SECONDS:
                key = str(target)
                if key in checkpoints or age_seconds + 1e-9 < target:
                    continue
                mark = {
                    "type": "candidate_checkpoint",
                    "schema_version": INTELLIGENCE_SCHEMA_VERSION,
                    "scan_id": active_id,
                    "ticker": ticker,
                    "asset": row.get("asset"),
                    "side": row.get("side"),
                    "target_offset_seconds": target,
                    "actual_offset_seconds": round(age_seconds, 3),
                    "captured_at": scanned_at,
                    "mark": _mark_from_candidate(current),
                }
                baseline_pricing = (row.get("card") or {}).get("pricing") or {}
                baseline_probability = (row.get("card") or {}).get("probability") or {}
                current_entry = _finite(mark["mark"].get("entry_price_cents"))
                initial_entry = _finite(baseline_pricing.get("entry_price_cents"))
                current_probability = _finite(mark["mark"].get("calibrated_probability_yes"))
                initial_probability = _finite(baseline_probability.get("final_yes"))
                mark["changes"] = {
                    "entry_price_change_cents": round(current_entry - initial_entry, 4)
                    if current_entry is not None and initial_entry is not None
                    else None,
                    "calibrated_probability_change_pp": round(
                        current_probability - initial_probability,
                        4,
                    ) if current_probability is not None and initial_probability is not None else None,
                    "selected_side_adverse_price_move_cents": round(
                        current_entry - initial_entry,
                        4,
                    ) if current_entry is not None and initial_entry is not None else None,
                }
                checkpoints[key] = mark
                events.append(mark)
        settlement = settlement_map.get(ticker)
        if settlement:
            result = str(settlement.get("result") or "").lower()
            outcome = {
                "type": "candidate_settlement",
                "schema_version": INTELLIGENCE_SCHEMA_VERSION,
                "scan_id": active_id,
                "ticker": ticker,
                "asset": row.get("asset"),
                "side": row.get("side"),
                "result": result,
                "side_won": str(row.get("side") or "").lower() == result,
                "settled_at": settlement.get("settlement_ts") or scanned_at,
                "official_settlement_value": settlement.get("settlement_value"),
                "official_settlement_value_field": settlement.get("settlement_value_field"),
                "settlement_margin": settlement.get("settlement_margin"),
                "settlement_margin_bps": settlement.get("settlement_margin_bps"),
                "counterfactual_unit_tiers": _counterfactual_outcomes(row, result),
                "checkpoints": checkpoints,
            }
            price_moves = [
                _finite((checkpoint.get("changes") or {}).get("selected_side_adverse_price_move_cents"))
                for checkpoint in checkpoints.values()
            ]
            price_moves = [value for value in price_moves if value is not None]
            outcome["maximum_adverse_excursion_cents"] = max(price_moves) if price_moves else None
            outcome["maximum_favorable_excursion_cents"] = min(price_moves) if price_moves else None
            events.append(outcome)
            dynamic_selection = dynamic_selected.get(ticker) or {}
            if dynamic_selection.get("scan_id") == active_id:
                policy_outcome = settled_policy_outcome(
                    row.get("card") or {},
                    result,
                    policy_name="dynamic",
                )
                if policy_outcome:
                    policy_outcome["settled_at"] = outcome["settled_at"]
                    dynamic_outcomes.append(policy_outcome)
                    events.append({
                        "type": "dynamic_qualification_settlement",
                        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
                        **policy_outcome,
                    })
                dynamic_selected.pop(ticker, None)
            current_selection = current_policy_selected.get(ticker) or {}
            if current_selection.get("scan_id") == active_id:
                current_outcome = settled_policy_outcome(
                    row.get("card") or {},
                    result,
                    policy_name="current",
                )
                if current_outcome:
                    current_outcome["settled_at"] = outcome["settled_at"]
                    current_policy_outcomes.append(current_outcome)
                current_policy_selected.pop(ticker, None)
            counterfactual_selection = counterfactual_selected.get(ticker) or {}
            if counterfactual_selection.get("scan_id") == active_id:
                counterfactual_outcome = _counterfactual_market_outcome(
                    row,
                    result,
                    outcome["settled_at"],
                )
                if counterfactual_outcome:
                    counterfactual_outcomes.append(counterfactual_outcome)
                    events.append(counterfactual_outcome)
                counterfactual_selected.pop(ticker, None)
            remove.append(active_id)
            stats["settled_records"] = int(stats.get("settled_records") or 0) + 1
        elif age_seconds > 7200:
            events.append({
                "type": "candidate_lifecycle_expired",
                "schema_version": INTELLIGENCE_SCHEMA_VERSION,
                "scan_id": active_id,
                "ticker": ticker,
                "expired_at": scanned_at,
                "reason": "settlement_not_observed_within_two_hours",
                "checkpoints": checkpoints,
            })
            if (dynamic_selected.get(ticker) or {}).get("scan_id") == active_id:
                dynamic_selected.pop(ticker, None)
            if (current_policy_selected.get(ticker) or {}).get("scan_id") == active_id:
                current_policy_selected.pop(ticker, None)
            if (counterfactual_selected.get(ticker) or {}).get("scan_id") == active_id:
                counterfactual_selected.pop(ticker, None)
            remove.append(active_id)
    for key in remove:
        active.pop(key, None)

    dynamic_outcomes[:] = dynamic_outcomes[-5000:]
    current_policy_outcomes[:] = current_policy_outcomes[-5000:]
    counterfactual_outcomes[:] = counterfactual_outcomes[-5000:]
    validation = dynamic_policy_validation(
        settings or {},
        dynamic_outcomes,
        current_policy_outcomes,
    )
    counterfactual_validation = _counterfactual_validation_summary(
        counterfactual_outcomes,
        dynamic_config.get("minimum_independent_markets", 100),
    )
    prior_status = str(dynamic_state.get("status") or "shadow")
    qualified = bool(validation.get("qualified"))
    desired_status = (
        "active"
        if qualified and dynamic_config.get("auto_promote")
        else "shadow"
        if dynamic_config.get("enabled")
        else "disabled"
    )
    if desired_status != prior_status:
        transition = {
            "changed_at": scanned_at,
            "from_status": prior_status,
            "to_status": desired_status,
            "reason": (
                "locked_validation_passed"
                if desired_status == "active"
                else "validation_regressed"
                if prior_status == "active"
                else "policy_disabled"
            ),
            "independent_markets": validation.get("independent_markets"),
        }
        dynamic_state.setdefault("transitions", []).append(transition)
        events.append({
            "type": "dynamic_qualification_transition",
            "schema_version": INTELLIGENCE_SCHEMA_VERSION,
            **transition,
        })
    dynamic_state.update({
        "version": DYNAMIC_POLICY_VERSION,
        "policy_hash": dynamic_config.get("policy_hash"),
        "status": desired_status,
        "qualified_for_activation": qualified,
        "affects_execution": desired_status == "active",
        "activation_reason": (
            "locked_validation_passed"
            if desired_status == "active"
            else "collecting_independent_markets"
            if validation.get("independent_markets", 0)
            < dynamic_config.get("minimum_independent_markets", 100)
            else "locked_validation_not_passed"
        ),
        "independent_markets": validation.get("independent_markets", 0),
        "minimum_independent_markets": dynamic_config.get("minimum_independent_markets"),
        "activation_checks": validation.get("checks") or {},
        "validation": validation,
        "counterfactual_validation": counterfactual_validation,
        "updated_at": scanned_at,
        "selected": dynamic_selected,
        "current_policy_selected": current_policy_selected,
        "counterfactual_selected": counterfactual_selected,
        "outcomes": dynamic_outcomes,
        "current_policy_outcomes": current_policy_outcomes,
        "counterfactual_outcomes": counterfactual_outcomes,
        "transitions": list(dynamic_state.get("transitions") or [])[-100:],
    })

    cards = []
    for candidate in candidates or []:
        candidate_id = f"{scan_id}:{candidate.get('ticker')}"
        card = candidate_card(
            candidate,
            scan_id=candidate_id,
            scanned_at=scanned_at,
            btc_context=btc_context,
        )
        card["batch_scan_id"] = scan_id
        candidate["scan_intelligence"] = card
        cards.append(card)
        event = {
            "type": "candidate_scan",
            **card,
        }
        events.append(event)
        active[candidate_id] = {
            "scan_id": candidate_id,
            "scanned_at": scanned_at,
            "ticker": candidate.get("ticker"),
            "asset": candidate.get("asset"),
            "side": candidate.get("side"),
            "close_time": candidate.get("close_time"),
            "card": card,
            "checkpoints": {},
        }
        dynamic_review = card.get("dynamic_qualification") or {}
        ticker = str(card.get("ticker") or "")
        already_observed = any(
            str(row.get("ticker") or "") == ticker
            for row in dynamic_outcomes
        )
        if (
            ticker
            and not already_observed
            and ticker not in dynamic_selected
            and dynamic_review.get("policy_hash") == dynamic_config.get("policy_hash")
            and dynamic_review.get("shadow_eligible")
        ):
            dynamic_selected[ticker] = {
                "scan_id": candidate_id,
                "selected_at": scanned_at,
                "target_units": dynamic_review.get("target_units"),
                "lane": dynamic_review.get("lane"),
            }
            events.append({
                "type": "dynamic_qualification_selected",
                "schema_version": INTELLIGENCE_SCHEMA_VERSION,
                "scan_id": candidate_id,
                "ticker": ticker,
                "selected_at": scanned_at,
                "target_units": dynamic_review.get("target_units"),
                "lane": dynamic_review.get("lane"),
                "policy_hash": dynamic_review.get("policy_hash"),
            })
        current_units = card.get("unit_sizing") or {}
        counterfactual_already_observed = any(
            str(row.get("ticker") or "") == ticker
            for row in counterfactual_outcomes
        )
        if (
            ticker
            and not counterfactual_already_observed
            and ticker not in counterfactual_selected
            and dynamic_review.get("policy_hash") == dynamic_config.get("policy_hash")
            and _counterfactual_collection_eligible(card)
        ):
            counterfactual_selected[ticker] = {
                "scan_id": candidate_id,
                "selected_at": scanned_at,
                "shadow_policy_selected": bool(dynamic_review.get("shadow_eligible")),
                "target_units": dynamic_review.get("target_units"),
            }
            events.append({
                "type": "dynamic_qualification_counterfactual_selected",
                "schema_version": INTELLIGENCE_SCHEMA_VERSION,
                "scan_id": candidate_id,
                "ticker": ticker,
                "selected_at": scanned_at,
                "shadow_policy_selected": bool(dynamic_review.get("shadow_eligible")),
                "target_units": dynamic_review.get("target_units"),
                "policy_hash": dynamic_review.get("policy_hash"),
                "affects_execution": False,
            })
        if (
            ticker
            and ticker not in current_policy_selected
            and current_units.get("qualification_policy")
            == "legacy_probability_edge_tiers"
            and current_units.get("eligible")
            and (card.get("campaign_review") or {}).get("eligible")
            and card.get("decision") in {"eligible", "placed"}
        ):
            current_policy_selected[ticker] = {
                "scan_id": candidate_id,
                "selected_at": scanned_at,
                "target_units": current_units.get("target_units"),
            }

    if len(active) > max(1, int(max_active)):
        ordered = sorted(
            active.items(),
            key=lambda item: str(item[1].get("scanned_at") or ""),
        )
        active.clear()
        active.update(ordered[-max(1, int(max_active)):])

    written = _append_jsonl(ledger_file, events)
    stats.update({
        "scan_count": int(stats.get("scan_count") or 0) + 1,
        "candidate_records": int(stats.get("candidate_records") or 0) + len(cards),
        "lifecycle_records": int(stats.get("lifecycle_records") or 0) + max(0, written - len(cards)),
        "active_records": len(active),
        "last_scan_id": scan_id,
        "last_scan_at": scanned_at,
        "last_candidate_count": len(cards),
    })
    state.update({
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "updated_at": scanned_at,
        "active": active,
        "stats": stats,
        "dynamic_policy": dynamic_state,
    })
    _atomic_write(state_file, state)
    observed_markets = int(
        counterfactual_validation.get("observed_independent_markets") or 0
    )
    minimum_observed = int(
        counterfactual_validation.get("minimum_observed_markets") or 100
    )
    readiness = {
        "status": (
            "collecting"
            if observed_markets < minimum_observed
            else "eligible_for_shadow_ablation"
        ),
        "minimum_independent_settled_markets": minimum_observed,
        "observed_independent_settled_markets": observed_markets,
        "newly_observed_settled_markets": len({
            str(row.get("ticker") or "")
            for row in settlements or []
            if row.get("ticker")
        }),
        "affects_live_probability": False,
        "affects_live_entry_rules": dynamic_state.get("status") == "active",
        "execution_tier_pricing_affects_sizing": True,
        "promotion_rule": "market_level_walk_forward_brier_log_loss_calibration_and_after_fee_roi",
    }
    return {
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "scan_id": scan_id,
        "written_records": written,
        "candidate_records": len(cards),
        "lifecycle_records": max(0, written - len(cards)),
        "active_records": len(active),
        "stats": dict(stats),
        "validation": readiness,
        "dynamic_policy": {
            key: dynamic_state.get(key)
            for key in (
                "version",
                "policy_hash",
                "status",
                "qualified_for_activation",
                "affects_execution",
                "activation_reason",
                "independent_markets",
                "minimum_independent_markets",
                "activation_checks",
                "validation",
                "counterfactual_validation",
                "updated_at",
                "transitions",
            )
        },
        "recent_candidates": cards[:25],
    }


def _tail_lines(path, maximum_lines=50000, maximum_bytes=128 * 1024 * 1024):
    path = Path(path)
    if not path.exists():
        return []
    size = path.stat().st_size
    read_size = min(size, max(4096, int(maximum_bytes)))
    with path.open("rb") as handle:
        handle.seek(max(0, size - read_size))
        payload = handle.read(read_size)
    if read_size < size:
        first_newline = payload.find(b"\n")
        payload = payload[first_newline + 1 :] if first_newline >= 0 else b""
    return payload.decode("utf-8", errors="replace").splitlines()[-max(1, int(maximum_lines)):]


def intelligence_history(path, *, minutes=60.0, limit=50000, now=None):
    """Load all recent candidate scans, rather than event-log top-N samples."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=max(0.0, min(10080.0, float(minutes or 0))))
    rows = deque(maxlen=max(1, int(limit)))
    for line in _tail_lines(path, maximum_lines=max(50000, int(limit) * 4)):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "candidate_scan":
            continue
        scanned = _parse_time(event.get("scanned_at"))
        if scanned and scanned < cutoff:
            continue
        pricing = event.get("pricing") or {}
        rows.append({
            "scan_id": event.get("scan_id"),
            "scanned_at": event.get("scanned_at"),
            "asset": event.get("asset"),
            "ticker": event.get("ticker"),
            "side": event.get("side"),
            "entry_price": pricing.get("entry_price_cents"),
            "edge": pricing.get("expected_edge_cents"),
            "expected_edge": pricing.get("expected_edge_cents"),
            "edge_low": pricing.get("edge_low_cents"),
            "confidence": pricing.get("probability_net_edge_positive"),
            "probability_net_edge_positive": pricing.get("probability_net_edge_positive"),
            "probability": {
                "p_yes": (event.get("probability") or {}).get("final_yes"),
                "p_low": (event.get("probability") or {}).get("low_yes"),
                "p_high": (event.get("probability") or {}).get("high_yes"),
                "market_implied_yes": (event.get("probability") or {}).get("market_implied_yes"),
            },
            "data_quality": event.get("data_quality") or {},
            "decision": event.get("decision"),
            "skip_reasons": event.get("skip_reasons") or [],
            "campaign_review": event.get("campaign_review") or {},
            "crypto_units": event.get("unit_sizing") or {},
            "disagreement_guard": event.get("disagreement_guard") or {},
            "directional_confirmation": event.get("directional_confirmation") or {},
            "dynamic_qualification": event.get("dynamic_qualification") or {},
            "scan_intelligence": event,
            "what_would_change_decision": event.get("what_would_change_decision") or [],
        })
    result = list(reversed(rows))
    return {
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "rows": result,
        "minutes": float(minutes or 0),
        "available_rows": len(result),
        "source": str(path),
    }
