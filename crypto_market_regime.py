"""Multi-horizon market regime research and bounded live adjustment for 15-minute markets."""

from __future__ import annotations

import json
import math
import os
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path


VERSION = "market-regime-v3"
CRYPTO_ASSETS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "LINK", "LTC", "BCH", "SUI", "DOT"}
FORECAST_HORIZONS = {"1h": 60 * 60, "4h": 4 * 60 * 60, "24h": 24 * 60 * 60}
FORECAST_MINIMUM_WINDOWS = 100


def number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) else float(default)
    except (TypeError, ValueError):
        return float(default)


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, number(value)))


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def atomic_write_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return {} if default is None else default


def log_returns(closes):
    values = [number(value) for value in closes or [] if number(value) > 0]
    return [math.log(values[index] / values[index - 1]) for index in range(1, len(values))]


def window_return(returns, periods):
    return sum(returns[-max(1, int(periods)):]) if returns else 0.0


def ema(values, period):
    values = [number(value) for value in values or [] if number(value) > 0]
    if not values:
        return 0.0
    alpha = 2.0 / (max(1, int(period)) + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def rsi(values, period=14):
    values = [number(value) for value in values or [] if number(value) > 0]
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    sample = changes[-max(2, int(period)):]
    if len(sample) < 2:
        return 50.0
    average_gain = sum(max(change, 0.0) for change in sample) / len(sample)
    average_loss = sum(max(-change, 0.0) for change in sample) / len(sample)
    if average_loss <= 1e-12:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def downsample(values, step):
    values = list(values or [])
    step = max(1, int(step))
    return values[step - 1::step] or values[-1:]


def trend_efficiency(values, periods):
    sample = [number(value) for value in (values or [])[-max(2, int(periods)):]]
    if len(sample) < 3:
        return 0.0
    path = sum(abs(sample[index] - sample[index - 1]) for index in range(1, len(sample)))
    return clamp(abs(sample[-1] - sample[0]) / path, 0.0, 1.0) if path > 0 else 0.0


def normalized_momentum(momentum, volatility, horizon_scale=1.0):
    denominator = max(0.00025, number(volatility) * max(1.0, number(horizon_scale)) * 1.5)
    return clamp(number(momentum) / denominator)


def _direction(score, probability_up, available=True):
    if not available:
        return "neutral"
    if number(score) >= 0.14 and number(probability_up, 0.5) >= 0.56:
        return "up"
    if number(score) <= -0.14 and number(probability_up, 0.5) <= 0.44:
        return "down"
    return "neutral"


def _component_agreement(components, score):
    components = [number(value) for value in components or [] if abs(number(value)) >= 0.025]
    if not components or abs(number(score)) < 1e-9:
        return 0.5
    expected_positive = number(score) > 0
    matching = sum(1 for value in components if (value > 0) == expected_positive)
    return clamp(matching / len(components), 0.0, 1.0)


def _calibration_bin(evaluation, raw_probability):
    for row in (evaluation or {}).get("calibration_bins") or []:
        if number(row.get("lower")) <= raw_probability <= number(row.get("upper"), 1.0):
            return row
    return None


def _forecast(score, data_quality, agreement, available, evaluation=None):
    """Return a conservative probability with forward-only empirical shrinkage.

    The score-to-probability map is intentionally modest. Once a score bin has
    at least 30 matured forecasts, its observed frequency receives a bounded
    weight; sparse history can never manufacture high confidence.
    """
    if not available:
        return {
            "available": False,
            "status": "insufficient_history",
            "direction": "neutral",
            "score": round(number(score), 5),
            "probability_up": 0.5,
            "directional_confidence": 0.5,
            "agreement": round(number(agreement, 0.5), 4),
            "data_quality": round(number(data_quality), 4),
        }
    raw_probability = _sigmoid(2.4 * number(score))
    quality_scale = 0.55 + 0.45 * clamp(data_quality, 0.0, 1.0)
    agreement_scale = 0.65 + 0.35 * clamp(agreement, 0.0, 1.0)
    probability = 0.5 + (raw_probability - 0.5) * quality_scale * agreement_scale
    validated_windows = int(number((evaluation or {}).get("independent_windows")))
    validation_scale = 0.55 + 0.45 * min(1.0, validated_windows / FORECAST_MINIMUM_WINDOWS)
    probability = 0.5 + (probability - 0.5) * validation_scale
    calibration = _calibration_bin(evaluation or {}, probability)
    calibration_sample = int(number((calibration or {}).get("forecasts")))
    if calibration_sample >= 30:
        empirical = number((calibration or {}).get("observed_up_rate"), 0.5)
        empirical = (empirical * calibration_sample + 5.0) / (calibration_sample + 10.0)
        calibration_weight = min(0.50, calibration_sample / 400.0)
        probability = (1.0 - calibration_weight) * probability + calibration_weight * empirical
    probability = max(0.05, min(0.95, probability))
    direction = _direction(score, probability, available=True)
    return {
        "available": True,
        "status": "validated" if validated_windows >= FORECAST_MINIMUM_WINDOWS else "calibrating",
        "direction": direction,
        "score": round(number(score), 5),
        "probability_up": round(probability, 4),
        "directional_confidence": round(max(probability, 1.0 - probability), 4),
        "agreement": round(number(agreement, 0.5), 4),
        "data_quality": round(number(data_quality), 4),
        "calibration_sample": calibration_sample,
    }


def _weighted_source_flow(source_rows, field):
    values = [number(source.get(field)) for source in source_rows if source.get(field) is not None]
    return clamp(sum(values) / len(values)) if values else 0.0


def _perps_by_asset(perps_report):
    return {
        str(row.get("asset") or "").upper(): row
        for row in (perps_report or {}).get("top_candidates") or []
        if row.get("asset")
    }


def _asset_regime(asset, row, perps, news, now, calibration=None):
    closes = [number(value) for value in (row or {}).get("closes") or [] if number(value) > 0]
    spot = number((row or {}).get("spot"), closes[-1] if closes else 0.0)
    if spot > 0 and (not closes or abs(closes[-1] / spot - 1.0) > 1e-9):
        closes.append(spot)
    closes_15m = [number(value) for value in (row or {}).get("closes_15m") or [] if number(value) > 0]
    if spot > 0 and closes_15m and abs(closes_15m[-1] / spot - 1.0) > 1e-9:
        closes_15m.append(spot)
    returns = log_returns(closes)
    minute_vol = statistics.pstdev(returns[-60:]) if len(returns) >= 8 else 0.0005
    minute_vol = max(0.00015, minute_vol)
    momentum_15m = window_return(returns, 15)
    momentum_1h = window_return(returns, 60)
    momentum_4h = window_return(returns, 240)
    mom_1h_score = normalized_momentum(momentum_1h, minute_vol * math.sqrt(60.0))
    mom_4h_score = normalized_momentum(momentum_4h, minute_vol * math.sqrt(240.0))

    fast_1h = ema(closes[-90:], 15) or spot
    slow_1h = ema(closes[-300:], 60) or spot
    ema_gap_1h = math.log(max(fast_1h, 1e-9) / max(slow_1h, 1e-9)) if fast_1h > 0 and slow_1h > 0 else 0.0
    ema_score_1h = normalized_momentum(ema_gap_1h, minute_vol * math.sqrt(60.0))
    fast_4h = ema(closes[-300:], 60) or spot
    slow_4h = ema(closes[-360:], 240) or spot
    ema_gap_4h = math.log(max(fast_4h, 1e-9) / max(slow_4h, 1e-9)) if fast_4h > 0 and slow_4h > 0 else 0.0
    ema_score_4h = normalized_momentum(ema_gap_4h, minute_vol * math.sqrt(240.0))
    efficiency_1h = trend_efficiency(closes, 60)
    efficiency_4h = trend_efficiency(closes, 240)
    rsi_1h = rsi(downsample(closes, 5), 14)
    rsi_4h = rsi(downsample(closes, 15), 14)
    rsi_trend = clamp((rsi_1h - 50.0) / 25.0)

    micro = (row or {}).get("microstructure") or {}
    source_rows = [micro.get("coinbase") or {}, micro.get("kraken") or {}]
    flow_5m = _weighted_source_flow(source_rows, "trade_flow_300s")
    flow_15m = _weighted_source_flow(source_rows, "trade_flow_900s")
    flow_1h = _weighted_source_flow(source_rows, "trade_flow_3600s")
    composite_flow = number(micro.get("underlying_flow_score"))
    flow_score_1h = clamp(0.15 * flow_5m + 0.25 * flow_15m + 0.45 * flow_1h + 0.15 * composite_flow)
    flow_score_4h = clamp(0.20 * flow_15m + 0.60 * flow_1h + 0.20 * composite_flow)
    buy_sell_5m = sum(
        number(source.get("trade_notional_300s")) * number(source.get("trade_flow_300s"))
        for source in source_rows
    )
    total_5m = sum(number(source.get("trade_notional_300s")) for source in source_rows)

    perp = perps or {}
    perp_vol = max(0.0005, number(perp.get("hourly_vol"), minute_vol * math.sqrt(60.0)))
    perp_1h = normalized_momentum(perp.get("momentum_1h"), perp_vol)
    perp_4h = normalized_momentum(perp.get("momentum_4h"), perp_vol * 2.0)
    oi_change = clamp(number(perp.get("open_interest_change_12h")) / 0.08)
    funding = clamp(number(perp.get("funding_rate")) / 0.002)
    perp_confirmation_1h = clamp(0.70 * perp_1h + 0.20 * perp_1h * max(0.0, oi_change) - 0.10 * funding)
    perp_confirmation_4h = clamp(0.70 * perp_4h + 0.20 * perp_4h * max(0.0, oi_change) - 0.10 * funding)

    news_score = clamp((news or {}).get("score"))
    trend_core_1h = clamp(
        0.38 * mom_1h_score
        + 0.22 * ema_score_1h
        + 0.10 * efficiency_1h * (1.0 if momentum_1h >= 0 else -1.0)
        + 0.20 * flow_score_1h
        + 0.10 * perp_confirmation_1h
    )
    # RSI follows persistent trends but becomes a small contrarian input in ranges.
    rsi_component = rsi_trend if efficiency_1h >= 0.28 else -0.35 * rsi_trend
    score_1h = clamp(0.90 * trend_core_1h + 0.07 * rsi_component + 0.03 * news_score)
    score_4h = clamp(
        0.48 * mom_4h_score
        + 0.22 * ema_score_4h
        + 0.12 * efficiency_4h * (1.0 if momentum_4h >= 0 else -1.0)
        + 0.15 * perp_confirmation_4h
        + 0.03 * news_score
    )

    returns_15m = log_returns(closes_15m)
    quarter_hour_vol = statistics.pstdev(returns_15m[-96:]) if len(returns_15m) >= 16 else 0.001
    quarter_hour_vol = max(0.0003, quarter_hour_vol)
    momentum_12h = window_return(returns_15m, 48)
    momentum_24h = window_return(returns_15m, 96)
    momentum_72h = window_return(returns_15m, 288) if len(returns_15m) >= 192 else 0.0
    mom_12h_score = normalized_momentum(momentum_12h, quarter_hour_vol * math.sqrt(48.0))
    mom_24h_score = normalized_momentum(momentum_24h, quarter_hour_vol * math.sqrt(96.0))
    mom_72h_score = normalized_momentum(momentum_72h, quarter_hour_vol * math.sqrt(288.0)) if momentum_72h else 0.0
    fast_24h = ema(closes_15m[-192:], 16) or spot
    slow_24h = ema(closes_15m[-300:], 96) or spot
    ema_gap_24h = math.log(max(fast_24h, 1e-9) / max(slow_24h, 1e-9)) if fast_24h > 0 and slow_24h > 0 else 0.0
    ema_score_24h = normalized_momentum(ema_gap_24h, quarter_hour_vol * math.sqrt(96.0))
    efficiency_24h = trend_efficiency(closes_15m, 96)
    rsi_24h = rsi(closes_15m, 48)
    perp_12h = normalized_momentum(perp.get("momentum_12h"), perp_vol * math.sqrt(12.0))
    perp_24h = normalized_momentum(perp.get("momentum_24h"), perp_vol * math.sqrt(24.0))
    perp_confirmation_24h = clamp(0.45 * perp_12h + 0.45 * perp_24h - 0.10 * funding)
    score_24h = clamp(
        0.18 * mom_12h_score
        + 0.34 * mom_24h_score
        + 0.10 * mom_72h_score
        + 0.18 * ema_score_24h
        + 0.10 * efficiency_24h * (1.0 if momentum_24h >= 0 else -1.0)
        + 0.07 * perp_confirmation_24h
        + 0.03 * news_score
    )

    shock_z = abs(momentum_15m) / max(0.00025, minute_vol * math.sqrt(15.0))
    if shock_z >= 2.75:
        regime = "shock"
    elif max(efficiency_1h, efficiency_4h) >= 0.32 and max(abs(score_1h), abs(score_4h)) >= 0.22:
        regime = "trending"
    elif score_1h * score_4h < -0.04:
        regime = "reversal_risk"
    else:
        regime = "ranging"

    fetched_at = parse_time((row or {}).get("fetched_at"))
    age_seconds = max(0.0, (now - fetched_at).total_seconds()) if fetched_at else None
    history_coverage_1h = clamp(len(closes) / 60.0, 0.0, 1.0)
    history_coverage_4h = clamp(len(closes) / 240.0, 0.0, 1.0)
    history_coverage_24h = clamp(len(closes_15m) / 96.0, 0.0, 1.0)
    source_coverage = clamp(number(micro.get("settlement_proxy_source_count")) / 3.0, 0.0, 1.0)
    freshness = 1.0 if age_seconds is not None and age_seconds <= 90 else 0.5 if age_seconds is not None and age_seconds <= 300 else 0.2
    reliability_1h = clamp(0.45 * history_coverage_1h + 0.25 * source_coverage + 0.20 * freshness + 0.10 * bool(perp), 0.0, 1.0)
    reliability_4h = clamp(0.55 * history_coverage_4h + 0.15 * source_coverage + 0.20 * freshness + 0.10 * bool(perp), 0.0, 1.0)
    reliability_24h = clamp(0.65 * history_coverage_24h + 0.10 * source_coverage + 0.15 * freshness + 0.10 * bool(perp), 0.0, 1.0)
    reliability = min(reliability_1h, reliability_4h)

    agreement_1h = _component_agreement(
        [mom_1h_score, ema_score_1h, flow_score_1h, perp_confirmation_1h, rsi_component],
        score_1h,
    )
    agreement_4h = _component_agreement(
        [mom_4h_score, ema_score_4h, flow_score_4h, perp_confirmation_4h],
        score_4h,
    )
    agreement_24h = _component_agreement(
        [mom_12h_score, mom_24h_score, mom_72h_score, ema_score_24h, perp_confirmation_24h],
        score_24h,
    )
    calibration = calibration or {}
    forecast_1h = _forecast(
        score_1h, reliability_1h, agreement_1h, len(closes) >= 60,
        (calibration.get("horizons") or {}).get("1h"),
    )
    forecast_4h = _forecast(
        score_4h, reliability_4h, agreement_4h, len(closes) >= 240,
        (calibration.get("horizons") or {}).get("4h"),
    )
    forecast_24h = _forecast(
        score_24h, reliability_24h, agreement_24h, len(closes_15m) >= 96,
        (calibration.get("horizons") or {}).get("24h"),
    )
    combined = clamp(0.65 * score_1h + 0.35 * score_4h)
    combined_probability = 0.65 * forecast_1h["probability_up"] + 0.35 * forecast_4h["probability_up"]
    direction = _direction(combined, combined_probability, forecast_1h["available"] and forecast_4h["available"])
    return {
        "asset": asset,
        "spot": round(spot, 10),
        "direction": direction,
        "direction_1h": forecast_1h["direction"],
        "direction_4h": forecast_4h["direction"],
        "direction_24h": forecast_24h["direction"],
        "regime": regime,
        "score_1h": round(score_1h, 5),
        "score_4h": round(score_4h, 5),
        "score_24h": round(score_24h, 5),
        "probability_up_1h": forecast_1h["probability_up"],
        "probability_up_4h": forecast_4h["probability_up"],
        "probability_up_24h": forecast_24h["probability_up"],
        "confidence_1h": forecast_1h["directional_confidence"],
        "confidence_4h": forecast_4h["directional_confidence"],
        "confidence_24h": forecast_24h["directional_confidence"],
        "forecast_horizons": {"1h": forecast_1h, "4h": forecast_4h, "24h": forecast_24h},
        "combined_score": round(combined, 5),
        "strength": round(abs(combined), 5),
        "reliability": round(reliability, 5),
        "features": {
            "momentum_15m": round(momentum_15m, 7),
            "momentum_1h": round(momentum_1h, 7),
            "momentum_4h": round(momentum_4h, 7),
            "momentum_12h": round(momentum_12h, 7),
            "momentum_24h": round(momentum_24h, 7),
            "momentum_72h": round(momentum_72h, 7),
            "ema_gap": round(ema_gap_1h, 7),
            "ema_gap_1h": round(ema_gap_1h, 7),
            "ema_gap_4h": round(ema_gap_4h, 7),
            "ema_gap_24h": round(ema_gap_24h, 7),
            "rsi_1h": round(rsi_1h, 2),
            "rsi_4h": round(rsi_4h, 2),
            "rsi_24h": round(rsi_24h, 2),
            "trend_efficiency_1h": round(efficiency_1h, 4),
            "trend_efficiency_4h": round(efficiency_4h, 4),
            "trend_efficiency_24h": round(efficiency_24h, 4),
            "flow_score": round(flow_score_1h, 5),
            "flow_score_1h": round(flow_score_1h, 5),
            "flow_score_4h": round(flow_score_4h, 5),
            "aggressive_buy_sell_notional_5m": round(buy_sell_5m, 2),
            "aggressive_total_notional_5m": round(total_5m, 2),
            "perps_momentum_1h": perp.get("momentum_1h"),
            "perps_momentum_4h": perp.get("momentum_4h"),
            "open_interest_change_12h": perp.get("open_interest_change_12h"),
            "funding_rate": perp.get("funding_rate"),
            "volume_zscore": perp.get("volume_zscore"),
            "shock_zscore": round(shock_z, 4),
        },
        "source_health": {
            "price_source": (row or {}).get("source"),
            "price_age_seconds": round(age_seconds, 2) if age_seconds is not None else None,
            "history_points": len(closes),
            "history_points_15m": len(closes_15m),
            "flow_sources": int(number(micro.get("settlement_proxy_source_count"))),
            "perps_available": bool(perp),
            "news_available": bool((news or {}).get("count")),
        },
    }


def build_regime_snapshot(
    asset_data,
    perps_report=None,
    news_context=None,
    ai_shadow=None,
    now=None,
    calibration=None,
):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    perps = _perps_by_asset(perps_report or {})
    news_context = news_context or {}
    assets = {}
    for asset, row in sorted((asset_data or {}).items()):
        if not row or not (row.get("closes") or []):
            continue
        asset_news = (news_context.get("by_asset") or {}).get(asset) or news_context.get("global") or {}
        assets[asset] = _asset_regime(asset, row, perps.get(asset), asset_news, now, calibration)

    crypto_rows = [row for asset, row in assets.items() if asset in CRYPTO_ASSETS]
    breadth = (
        sum(1 if row["combined_score"] > 0.10 else -1 if row["combined_score"] < -0.10 else 0 for row in crypto_rows)
        / max(1, len(crypto_rows))
    )
    btc_score = number((assets.get("BTC") or {}).get("combined_score"))
    global_score = clamp(0.60 * btc_score + 0.40 * breadth)
    average_1h = sum(number(row.get("score_1h")) for row in crypto_rows) / max(1, len(crypto_rows))
    average_4h = sum(number(row.get("score_4h")) for row in crypto_rows) / max(1, len(crypto_rows))
    daily_rows = [row for row in crypto_rows if ((row.get("forecast_horizons") or {}).get("24h") or {}).get("available")]
    average_24h = sum(number(row.get("score_24h")) for row in daily_rows) / max(1, len(daily_rows))
    global_1h = clamp(0.60 * number((assets.get("BTC") or {}).get("score_1h")) + 0.40 * average_1h)
    global_4h = clamp(0.60 * number((assets.get("BTC") or {}).get("score_4h")) + 0.40 * average_4h)
    btc_daily = assets.get("BTC") or {}
    btc_daily_available = ((btc_daily.get("forecast_horizons") or {}).get("24h") or {}).get("available")
    global_24h = clamp(
        0.60 * number(btc_daily.get("score_24h")) + 0.40 * average_24h
        if btc_daily_available else average_24h
    )
    def global_forecast(horizon, score, rows):
        available_rows = [
            row for row in rows
            if ((row.get("forecast_horizons") or {}).get(horizon) or {}).get("available")
        ]
        if not available_rows:
            return _forecast(score, 0.0, 0.5, False, (calibration or {}).get("horizons", {}).get(horizon))
        data_quality = sum(number(((row.get("forecast_horizons") or {}).get(horizon) or {}).get("data_quality")) for row in available_rows) / len(available_rows)
        agreement = sum(number(((row.get("forecast_horizons") or {}).get(horizon) or {}).get("agreement"), 0.5) for row in available_rows) / len(available_rows)
        return _forecast(score, data_quality, agreement, True, (calibration or {}).get("horizons", {}).get(horizon))

    forecast_1h = global_forecast("1h", global_1h, crypto_rows)
    forecast_4h = global_forecast("4h", global_4h, crypto_rows)
    forecast_24h = global_forecast("24h", global_24h, daily_rows)
    global_direction = "up" if global_score >= 0.18 else "down" if global_score <= -0.18 else "neutral"
    shock_count = sum(1 for row in crypto_rows if row.get("regime") == "shock")
    global_regime = (
        "shock" if shock_count >= 2
        else "trending" if sum(1 for row in crypto_rows if row.get("regime") == "trending") >= max(2, len(crypto_rows) // 2)
        else "mixed"
    )
    return {
        "version": VERSION,
        "mode": "shadow_only",
        "affects_execution": False,
        "generated_at": now.isoformat(timespec="seconds"),
        "global": {
            "direction": global_direction,
            "direction_1h": forecast_1h["direction"],
            "direction_4h": forecast_4h["direction"],
            "direction_24h": forecast_24h["direction"],
            "regime": global_regime,
            "score": round(global_score, 5),
            "score_1h": round(global_1h, 5),
            "score_4h": round(global_4h, 5),
            "score_24h": round(global_24h, 5),
            "probability_up_1h": forecast_1h["probability_up"],
            "probability_up_4h": forecast_4h["probability_up"],
            "probability_up_24h": forecast_24h["probability_up"],
            "confidence_1h": forecast_1h["directional_confidence"],
            "confidence_4h": forecast_4h["directional_confidence"],
            "confidence_24h": forecast_24h["directional_confidence"],
            "forecast_horizons": {"1h": forecast_1h, "4h": forecast_4h, "24h": forecast_24h},
            "breadth": round(breadth, 5),
            "asset_count": len(crypto_rows),
            "shock_asset_count": shock_count,
        },
        "assets": assets,
        "news": news_context,
        "ai_shadow": ai_shadow or {"enabled": False},
    }


def _logit(probability):
    probability = max(0.001, min(0.999, number(probability, 0.5)))
    return math.log(probability / (1.0 - probability))


def _sigmoid(value):
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, value))))


def annotate_candidates(
    candidates,
    snapshot,
    maximum_adjustment_pp=2.0,
    execution_enabled=False,
    live_maximum_adjustment_pp=0.75,
    live_maximum_age_minutes=10.0,
    live_minimum_reliability=0.65,
    now=None,
):
    maximum_adjustment_pp = max(0.0, number(maximum_adjustment_pp, 2.0))
    live_maximum_adjustment_pp = max(
        0.0,
        min(maximum_adjustment_pp, number(live_maximum_adjustment_pp, 0.75)),
    )
    live_maximum_age_minutes = max(1.0, number(live_maximum_age_minutes, 10.0))
    live_minimum_reliability = max(0.0, min(1.0, number(live_minimum_reliability, 0.65)))
    now = now or datetime.now(timezone.utc)
    snapshot_at = parse_time((snapshot or {}).get("generated_at"))
    snapshot_age_seconds = (
        max(0.0, (now - snapshot_at).total_seconds())
        if snapshot_at
        else None
    )
    snapshot_fresh = bool(
        snapshot_age_seconds is not None
        and snapshot_age_seconds <= live_maximum_age_minutes * 60.0
    )
    annotated = 0
    for candidate in candidates or []:
        asset = str(candidate.get("asset") or "").upper()
        regime = ((snapshot or {}).get("assets") or {}).get(asset)
        if not regime or not candidate.get("is_15m_market"):
            continue
        original_yes = max(0.1, min(99.9, number(candidate.get("model_prob_yes"), 50.0)))
        score = number(regime.get("combined_score"))
        reliability = number(regime.get("reliability"))
        ai_shadow = (snapshot or {}).get("ai_shadow") or {}
        ai_forecast = ((ai_shadow.get("assets") or {}).get(asset) or ai_shadow.get("global") or {})
        ai_direction = str(ai_forecast.get("direction_1h") or ai_forecast.get("direction") or "neutral").lower()
        ai_confidence = clamp(number(ai_forecast.get("confidence")), 0.0, 1.0)
        ai_score = (1.0 if ai_direction == "up" else -1.0 if ai_direction == "down" else 0.0) * ai_confidence
        # AI receives only a 10% research weight. It remains useful to measure,
        # but cannot overpower the deterministic price/flow regime.
        shadow_signal = clamp(0.90 * score + 0.10 * ai_score)
        proposed_yes = 100.0 * _sigmoid(_logit(original_yes / 100.0) + 0.08 * shadow_signal * reliability)
        yes_adjustment = max(-maximum_adjustment_pp, min(maximum_adjustment_pp, proposed_yes - original_yes))
        shadow_yes = original_yes + yes_adjustment
        selected_side = str(candidate.get("side") or "").lower()
        selected_adjustment = yes_adjustment if selected_side == "yes" else -yes_adjustment
        candidate["market_regime_shadow"] = {
            "version": VERSION,
            "mode": "shadow_only",
            "affects_execution": False,
            "snapshot_at": snapshot.get("generated_at"),
            "selected_side": selected_side,
            "direction": regime.get("direction"),
            "regime": regime.get("regime"),
            "score_1h": regime.get("score_1h"),
            "score_4h": regime.get("score_4h"),
            "score_24h": regime.get("score_24h"),
            "direction_24h": regime.get("direction_24h"),
            "probability_up_1h": regime.get("probability_up_1h"),
            "probability_up_4h": regime.get("probability_up_4h"),
            "probability_up_24h": regime.get("probability_up_24h"),
            "combined_score": regime.get("combined_score"),
            "shadow_signal": round(shadow_signal, 5),
            "strength": regime.get("strength"),
            "reliability": regime.get("reliability"),
            "original_yes_probability": round(original_yes, 4),
            "shadow_yes_probability": round(shadow_yes, 4),
            "yes_probability_adjustment_pp": round(yes_adjustment, 4),
            "selected_side_adjustment_pp": round(selected_adjustment, 4),
            "original_edge": round(number(candidate.get("edge")), 4),
            "shadow_adjusted_edge": round(number(candidate.get("edge")) + selected_adjustment, 4),
            "alignment": "aligned" if selected_adjustment > 0.05 else "conflicted" if selected_adjustment < -0.05 else "neutral",
            "news_risk": ((snapshot.get("news") or {}).get("global") or {}).get("risk_level", "normal"),
            "ai_direction": ai_direction,
            "ai_direction_4h": str(ai_forecast.get("direction_4h") or ai_forecast.get("direction") or "neutral").lower(),
            "ai_direction_24h": str(ai_forecast.get("direction_24h") or ai_forecast.get("direction") or "neutral").lower(),
            "ai_confidence": round(ai_confidence, 4),
            "ai_model": ai_shadow.get("model"),
        }
        asset_direction_1h = str(regime.get("direction_1h") or regime.get("direction") or "neutral").lower()
        asset_direction_4h = str(regime.get("direction_4h") or regime.get("direction") or "neutral").lower()
        live_reasons = []
        if not execution_enabled:
            live_reasons.append("execution_disabled")
        if not snapshot_fresh:
            live_reasons.append("snapshot_stale")
        if reliability < live_minimum_reliability:
            live_reasons.append("reliability_too_low")
        live_scale = 1.0
        scale_reasons = []
        if (
            asset_direction_1h in {"up", "down"}
            and asset_direction_4h in {"up", "down"}
            and asset_direction_1h != asset_direction_4h
        ):
            live_scale *= 0.5
            scale_reasons.append("horizon_disagreement_half_weight")
        if (
            ai_direction in {"up", "down"}
            and asset_direction_1h in {"up", "down"}
            and ai_direction != asset_direction_1h
        ):
            live_scale *= 0.5
            scale_reasons.append("ai_technical_disagreement_half_weight")
        news_risk = ((snapshot.get("news") or {}).get("global") or {}).get("risk_level", "normal")
        if str(news_risk).lower() in {"event", "high", "elevated"}:
            live_scale *= 0.75
            scale_reasons.append("event_news_reduced_weight")
        if str(regime.get("regime") or "").lower() == "shock":
            live_scale *= 0.5
            scale_reasons.append("shock_regime_half_weight")
        live_yes_adjustment = max(
            -live_maximum_adjustment_pp,
            min(live_maximum_adjustment_pp, yes_adjustment * live_scale),
        )
        applied = not live_reasons and abs(live_yes_adjustment) >= 0.0001
        original_edge = number(candidate.get("edge"))
        original_raw_edge = number(candidate.get("raw_edge"), original_edge)
        original_net_edge = number(candidate.get("net_edge"), original_edge)
        adjusted_yes = original_yes
        adjusted_edge = original_edge
        if applied:
            adjusted_yes = max(0.1, min(99.9, original_yes + live_yes_adjustment))
            adjusted_side_probability = adjusted_yes if selected_side == "yes" else 100.0 - adjusted_yes
            adjusted_raw_edge = adjusted_side_probability - number(candidate.get("entry_price"))
            fee_drag = number(candidate.get("estimated_fee_edge_pp"))
            adjusted_edge = adjusted_raw_edge - fee_drag
            candidate["model_prob_yes"] = round(adjusted_yes, 4)
            candidate["raw_edge"] = round(adjusted_raw_edge, 4)
            candidate["net_edge"] = round(adjusted_edge, 4)
            candidate["edge"] = round(adjusted_edge, 4)
        live_selected_adjustment = live_yes_adjustment if selected_side == "yes" else -live_yes_adjustment
        candidate["market_regime_live"] = {
            "version": VERSION,
            "mode": "live_bounded" if execution_enabled else "shadow_only",
            "affects_execution": bool(applied),
            "eligible": not live_reasons,
            "applied": bool(applied),
            "reasons": live_reasons,
            "scale_reasons": scale_reasons,
            "scale": round(live_scale, 4),
            "snapshot_at": snapshot.get("generated_at"),
            "snapshot_age_seconds": round(snapshot_age_seconds, 3) if snapshot_age_seconds is not None else None,
            "maximum_age_minutes": live_maximum_age_minutes,
            "minimum_reliability": live_minimum_reliability,
            "maximum_adjustment_pp": live_maximum_adjustment_pp,
            "selected_side": selected_side,
            "original_yes_probability": round(original_yes, 4),
            "adjusted_yes_probability": round(adjusted_yes, 4),
            "yes_probability_adjustment_pp": round(live_yes_adjustment if applied else 0.0, 4),
            "selected_side_adjustment_pp": round(live_selected_adjustment if applied else 0.0, 4),
            "original_raw_edge": round(original_raw_edge, 4),
            "original_net_edge": round(original_net_edge, 4),
            "original_edge": round(original_edge, 4),
            "adjusted_edge": round(adjusted_edge, 4),
            "direction": regime.get("direction"),
            "direction_1h": asset_direction_1h,
            "direction_4h": asset_direction_4h,
            "direction_24h": str(regime.get("direction_24h") or "neutral").lower(),
            "reliability": round(reliability, 4),
            "news_risk": news_risk,
            "ai_direction": ai_direction,
        }
        annotated += 1
    return annotated


def _forecast_evaluation(records):
    horizons = {}
    for horizon in FORECAST_HORIZONS:
        resolved = [row for row in records if row.get("horizon") == horizon and row.get("status") == "resolved"]
        directional = [row for row in resolved if row.get("direction") in {"up", "down"}]
        correct = sum(int(bool(row.get("correct"))) for row in directional)
        up_outcomes = sum(int(number(row.get("realized_return")) > 0) for row in resolved)
        brier_values = [
            (number(row.get("probability_up"), 0.5) - int(number(row.get("realized_return")) > 0)) ** 2
            for row in resolved
        ]
        bins = []
        for lower in (0.0, 0.4, 0.48, 0.52, 0.6):
            upper = {0.0: 0.4, 0.4: 0.48, 0.48: 0.52, 0.52: 0.6, 0.6: 1.0}[lower]
            selected = [
                row for row in resolved
                if lower <= number(row.get("probability_up"), 0.5) <= upper
                and not (lower != 0.6 and number(row.get("probability_up"), 0.5) == upper)
            ]
            bins.append({
                "lower": lower,
                "upper": upper,
                "forecasts": len(selected),
                "mean_probability_up": round(sum(number(row.get("probability_up"), 0.5) for row in selected) / max(1, len(selected)), 4),
                "observed_up_rate": round(sum(int(number(row.get("realized_return")) > 0) for row in selected) / max(1, len(selected)), 4),
            })
        windows = len({row.get("origin_bucket") for row in resolved})
        accuracy = correct / max(1, len(directional))
        up_rate = up_outcomes / max(1, len(resolved))
        baseline_accuracy = max(up_rate, 1.0 - up_rate) if resolved else 0.5
        brier = sum(brier_values) / max(1, len(brier_values))
        qualified = bool(
            windows >= FORECAST_MINIMUM_WINDOWS
            and len(directional) >= FORECAST_MINIMUM_WINDOWS
            and accuracy >= baseline_accuracy + 0.02
            and brier < 0.245
        )
        horizons[horizon] = {
            "status": "validated" if windows >= FORECAST_MINIMUM_WINDOWS else "collecting",
            "forecasts": len(resolved),
            "open_forecasts": sum(1 for row in records if row.get("horizon") == horizon and row.get("status") == "open"),
            "independent_windows": windows,
            "minimum_independent_windows": FORECAST_MINIMUM_WINDOWS,
            "directional_forecasts": len(directional),
            "directional_correct": correct,
            "directional_accuracy": round(accuracy, 4) if directional else None,
            "naive_direction_accuracy": round(baseline_accuracy, 4) if resolved else None,
            "brier_score": round(brier, 6) if resolved else None,
            "qualified_for_live": qualified,
            "calibration_bins": bins,
        }
    return {
        "version": VERSION,
        "method": "forward_only_non_overlapping_origin_windows",
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "horizons": horizons,
        "qualified_for_live": all(row.get("qualified_for_live") for row in horizons.values()),
    }


def update_forecast_records(existing_records, snapshot, now=None, maximum_records=20000):
    """Resolve matured predictions and add one origin per asset/horizon bucket."""
    now = now or parse_time((snapshot or {}).get("generated_at")) or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    records = [dict(row) for row in existing_records or [] if isinstance(row, dict)]
    current_assets = (snapshot or {}).get("assets") or {}
    for record in records:
        if record.get("status") != "open":
            continue
        due_at = parse_time(record.get("due_at"))
        current = current_assets.get(str(record.get("asset") or "").upper()) or {}
        end_spot = number(current.get("spot"))
        start_spot = number(record.get("start_spot"))
        if not due_at or now < due_at or end_spot <= 0 or start_spot <= 0:
            continue
        realized_return = end_spot / start_spot - 1.0
        actual_direction = "up" if realized_return > 0 else "down" if realized_return < 0 else "neutral"
        predicted_direction = str(record.get("direction") or "neutral")
        record.update({
            "status": "resolved",
            "resolved_at": now.isoformat(timespec="seconds"),
            "end_spot": round(end_spot, 10),
            "realized_return": round(realized_return, 8),
            "actual_direction": actual_direction,
            "correct": bool(predicted_direction == actual_direction) if predicted_direction in {"up", "down"} else None,
        })

    existing_ids = {str(row.get("id")) for row in records}
    for asset, current in sorted(current_assets.items()):
        if asset not in CRYPTO_ASSETS:
            continue
        spot = number(current.get("spot"))
        if spot <= 0:
            continue
        forecasts = current.get("forecast_horizons") or {}
        for horizon, seconds in FORECAST_HORIZONS.items():
            forecast = forecasts.get(horizon) or {}
            if not forecast.get("available") or number(forecast.get("data_quality")) < 0.60:
                continue
            origin_bucket = int(now.timestamp()) // seconds
            record_id = f"{asset}:{horizon}:{origin_bucket}"
            if record_id in existing_ids:
                continue
            records.append({
                "id": record_id,
                "asset": asset,
                "horizon": horizon,
                "origin_bucket": origin_bucket,
                "predicted_at": now.isoformat(timespec="seconds"),
                "due_at": datetime.fromtimestamp(now.timestamp() + seconds, tz=timezone.utc).isoformat(timespec="seconds"),
                "start_spot": round(spot, 10),
                "direction": forecast.get("direction"),
                "score": round(number(forecast.get("score")), 5),
                "probability_up": round(number(forecast.get("probability_up"), 0.5), 4),
                "data_quality": round(number(forecast.get("data_quality")), 4),
                "status": "open",
            })
            existing_ids.add(record_id)
    records = records[-max(100, int(maximum_records)):]
    return records, _forecast_evaluation(records)


def persist_snapshot(
    path,
    snapshot,
    maximum_history=2016,
    evaluation=None,
    forecast_records=None,
    forecast_evaluation=None,
):
    state = read_json(path, {"version": VERSION, "history": []})
    history = list(state.get("history") or [])
    last = history[-1] if history else {}
    if last.get("generated_at") != snapshot.get("generated_at"):
        history.append(snapshot)
    state.update({
        "version": VERSION,
        "latest": snapshot,
        "history": history[-max(1, int(maximum_history)):],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    if evaluation is not None:
        state["evaluation"] = evaluation
    if forecast_records is not None:
        state["forecast_records"] = forecast_records
    if forecast_evaluation is not None:
        state["forecast_evaluation"] = forecast_evaluation
    atomic_write_json(path, state)
    return state


def summarize_dataset(path, max_rows=50000):
    path = Path(path)
    if not path.exists():
        return {"settled": 0, "unique_markets": 0, "status": "collecting", "qualified_for_live": False}
    # The learning dataset can be hundreds of MB. Read only a bounded tail so a
    # five-minute research refresh never stalls the live scanner on old history.
    desired_lines = max(1, int(max_rows))
    chunks = []
    newline_count = 0
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        while position > 0 and newline_count <= desired_lines:
            size = min(1024 * 1024, position)
            position -= size
            handle.seek(position)
            chunk = handle.read(size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
    tail_lines = b"".join(reversed(chunks)).decode("utf-8", errors="replace").splitlines()[-desired_lines:]
    rows = []
    for line in tail_lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("label_yes") in {0, 1} and row.get("market_regime_shadow"):
            rows.append(row)
    if not rows:
        return {"settled": 0, "unique_markets": 0, "status": "collecting", "qualified_for_live": False}
    original_errors = []
    shadow_errors = []
    cohorts = {"aligned": [0, 0], "conflicted": [0, 0], "neutral": [0, 0]}
    for row in rows:
        regime = row.get("market_regime_shadow") or {}
        label = int(row["label_yes"])
        original = max(0.001, min(0.999, number(regime.get("original_yes_probability"), 50.0) / 100.0))
        shadow = max(0.001, min(0.999, number(regime.get("shadow_yes_probability"), 50.0) / 100.0))
        original_errors.append((original - label) ** 2)
        shadow_errors.append((shadow - label) ** 2)
        side = str(regime.get("selected_side") or "").lower()
        correct = (side == "yes" and label == 1) or (side == "no" and label == 0)
        cohort = regime.get("alignment") if regime.get("alignment") in cohorts else "neutral"
        cohorts[cohort][1] += 1
        cohorts[cohort][0] += int(correct)
    original_brier = sum(original_errors) / len(original_errors)
    shadow_brier = sum(shadow_errors) / len(shadow_errors)
    improvement = original_brier - shadow_brier
    settled = len(rows)
    unique_markets = len({str(row.get("ticker") or "") for row in rows if row.get("ticker")})
    return {
        "settled": settled,
        "unique_markets": unique_markets,
        "status": "validated" if unique_markets >= 300 else "collecting",
        "original_brier": round(original_brier, 6),
        "shadow_brier": round(shadow_brier, 6),
        "brier_improvement": round(improvement, 6),
        "qualified_for_live": bool(unique_markets >= 300 and improvement >= 0.002),
        "minimum_unique_markets_for_live": 300,
        "minimum_brier_improvement": 0.002,
        "cohorts": {
            label: {
                "wins": values[0],
                "settled": values[1],
                "win_rate": round(values[0] / max(1, values[1]), 4),
            }
            for label, values in cohorts.items()
        },
    }
