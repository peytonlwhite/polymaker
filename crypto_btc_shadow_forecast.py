"""BTC-only, forward-scored forecasts for the random-cycle shadow experiment.

Reuses the production regime features without changing its live adjustment.
Only contiguous, completed candles enter these forecasts. Calibration is local
to this cohort, asset and horizon; every stored probability precedes its label.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math

from crypto_market_regime import _asset_regime, number, parse_time

VERSION = "btc-closed-candle-forecast-v1"
HORIZONS = {"1h": 3600, "4h": 14400, "24h": 86400}


def completed_candles(candles, seconds, now):
    """Coinbase format: start, low, high, open, close, volume."""
    by_end = {}
    for row in candles or []:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        start, close = number(row[0], -1), number(row[4])
        if start < 0 or start % seconds or close <= 0:
            continue
        end = int(start) + seconds
        if end <= now.timestamp():
            by_end[end] = close
    if not by_end:
        return []
    end = max(by_end)
    if now.timestamp() - end >= seconds + 90:
        return []
    result = []
    while end in by_end:
        result.append({"end": end, "close": by_end[end]})
        end -= seconds
    return list(reversed(result))


def calibration(probability, records, now):
    eligible = [r for r in records if r.get("asset") == "BTC" and r.get("status") == "resolved"
                and parse_time(r.get("resolved_at")) is not None
                and parse_time(r["resolved_at"]) <= now
                and r.get("outcome") in (0, 1)]
    bucket = min(9, int(probability * 10))
    same = [r for r in eligible if min(9, int(number(r.get("raw_probability_up"), .5) * 10)) == bucket]
    calibrated = probability
    if len(same) >= 30:
        empirical = (sum(r["outcome"] for r in same) + 10) / (len(same) + 20)
        weight = min(.5, len(same) / 400)
        calibrated = (1-weight) * probability + weight * empirical
    brier = (sum((r["probability_up"]-r["outcome"])**2 for r in eligible)
             / len(eligible)) if eligible else None
    # Previously poor forward scores reduce conviction; never invert or select
    # a profitable-looking subperiod after observing its outcomes.
    if len(eligible) >= 100 and brier is not None and brier >= .25:
        calibrated = .5 + .25 * (calibrated-.5)
    return max(.35, min(.65, calibrated)), {
        "matured": len(eligible), "bin_sample": len(same), "brier": brier,
        "coin_flip_brier": .25 if eligible else None,
        "status": "forward_scored" if len(eligible) >= 100 else "uncalibrated",
    }


def forecast(minute_candles, quarter_candles, now, records=(), context=None):
    minute = completed_candles(minute_candles, 60, now)
    quarter = completed_candles(quarter_candles, 900, now)
    result = {"version": VERSION, "generated_at": now.isoformat(),
              "available": False, "direction": "neutral", "probability_up": .5,
              "status": "insufficient_closed_history", "horizons": {}}
    if len(minute) < 241 or len(quarter) < 97:
        return result
    row = {"closes": [r["close"] for r in minute],
           "closes_15m": [r["close"] for r in quarter],
           "spot": minute[-1]["close"], "source": "coinbase_completed_candles",
           "fetched_at": datetime.fromtimestamp(minute[-1]["end"], timezone.utc).isoformat()}
    base = _asset_regime("BTC", row, {}, {}, now)
    # The shared regime helper may append spot to its 15m vector. Recompute the
    # daily horizon from completed 15m closes only, with no synthetic last bar.
    daily_row = {**row, "spot": quarter[-1]["close"]}
    daily = _asset_regime("BTC", daily_row, {}, {}, now)
    base["forecast_horizons"]["24h"] = daily["forecast_horizons"]["24h"]
    current_context = context or {}
    context_at = parse_time(current_context.get("generated_at"))
    context_fresh = bool(context_at and 0 <= (now-context_at).total_seconds() <= 90)
    btc_context = (current_context.get("assets") or {}).get("BTC", {}) if context_fresh else {}
    horizon_rows = {}
    for name, seconds in HORIZONS.items():
        source = base["forecast_horizons"][name]
        probability = source["probability_up"]
        # Existing analysis supplies a small bounded context vote; it cannot
        # replace closed history or import pooled calibration confidence.
        context_score = number(btc_context.get("score_" + name))
        if btc_context:
            probability = .9 * probability + .1 * (.5 + .15 * max(-1, min(1, context_score)))
        selected = [r for r in records if r.get("horizon") == name]
        probability, evaluation = calibration(probability, selected, now)
        horizon_rows[name] = {
            **deepcopy(source), "raw_probability_up": round(
                .9 * source["probability_up"] + .1 * (.5 + .15 * max(-1, min(1, context_score)))
                if btc_context else source["probability_up"], 6),
            "probability_up": round(probability, 6),
            "directional_confidence": round(max(probability, 1-probability), 6),
            "direction": "up" if probability > .5 else "down" if probability < .5 else "neutral",
            "evaluation": evaluation, "target_seconds": seconds,
        }
    # Longer forecasts describe a different target. Project only a modest
    # drift signal to 15 minutes, then shrink disagreement and shocks.
    weights = {"1h": .65, "4h": .25, "24h": .10}
    votes = [(horizon_rows[h]["probability_up"]-.5) * math.sqrt(900/HORIZONS[h])
             for h in HORIZONS]
    drift = sum(weights[h] * votes[i] for i, h in enumerate(HORIZONS))
    agreement = max(sum(v > 0 for v in votes), sum(v < 0 for v in votes)) / 3
    shock = number((base.get("features") or {}).get("shock_zscore"))
    shrink = (.5 + .5 * agreement) * (.5 if shock >= 2.75 else 1)
    raw = .5 + drift * shrink
    probability, evaluation = calibration(raw, [r for r in records if r.get("horizon") == "15m"], now)
    result.update({"available": True, "status": evaluation["status"],
                   "direction": "up" if probability > .5 else "down" if probability < .5 else "neutral",
                   "raw_probability_up": round(raw, 6), "probability_up": round(probability, 6),
                   "confidence": round(max(probability, 1-probability), 6),
                   "agreement": agreement, "regime": base["regime"], "horizons": horizon_rows,
                   "evaluation": evaluation, "context_used": bool(btc_context),
                   "closed_minute_at": row["fetched_at"], "closed_minute_price": minute[-1]["close"],
                   "closed_minutes": len(minute), "closed_quarters": len(quarter),
                   "note": "Experimental probability; forward accuracy is not yet established."})
    return result


def advance_horizons(records, prediction, minute_candles, now):
    """One nonoverlapping BTC forecast per horizon; exact closed-candle labels.

    Missing endpoint candles expire a forecast rather than labeling at a later
    price. No late observation is silently substituted for its target time.
    """
    by_end = {r["end"]: r["close"] for r in completed_candles(minute_candles, 60, now)}
    for row in records:
        if row.get("horizon") not in HORIZONS or row.get("status") != "open":
            continue
        due = parse_time(row["due_at"])
        if now < due:
            continue
        price = by_end.get(int(due.timestamp()))
        if price is not None:
            row.update(status="resolved", outcome=int(price > row["start_price"]),
                       tie=price == row["start_price"], end_price=price, resolved_at=now.isoformat())
        elif (now-due).total_seconds() >= 90:
            row.update(status="expired", reason="target_candle_unavailable", resolved_at=now.isoformat())
    if not prediction.get("available"):
        return
    origin = parse_time(prediction["closed_minute_at"])
    for horizon, seconds in HORIZONS.items():
        previous = [r for r in records if r.get("horizon") == horizon]
        if previous and origin < max(parse_time(r["due_at"]) for r in previous):
            continue
        value = prediction["horizons"][horizon]
        records.append({"id": f"BTC:{horizon}:{int(origin.timestamp())}", "asset": "BTC",
                        "horizon": horizon, "status": "open", "captured_at": now.isoformat(),
                        "origin_at": origin.isoformat(), "due_at": (origin+timedelta(seconds=seconds)).isoformat(),
                        "start_price": prediction["closed_minute_price"],
                        "raw_probability_up": value["raw_probability_up"],
                        "probability_up": value["probability_up"]})
