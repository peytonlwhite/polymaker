"""Walk-forward learning and calibration for Kalshi 15-minute crypto markets."""

from __future__ import annotations

import json
import math
import os
import time
import uuid
import gzip
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

from crypto_scan_intelligence import (
    RESEARCH_FEATURE_SCHEMA_VERSION,
    research_feature_payload,
)
from crypto_pricing import kalshi_order_fee
from crypto_evidence import jsonl_sources, iter_jsonl


FEATURE_NAMES = (
    "minutes_fraction",
    "target_distance_sigma",
    "market_probability",
    "heuristic_probability",
    "momentum_1m_scaled",
    "momentum_5m_scaled",
    "momentum_15m_scaled",
    "momentum_60m_scaled",
    "minute_vol_scaled",
    "trend_bias",
    "fakeout_risk",
    "underlying_flow",
    "coinbase_book_imbalance",
    "coinbase_trade_flow_60s",
    "kraken_book_imbalance",
    "kraken_trade_flow_60s",
    "kalshi_book_imbalance",
    "kalshi_trade_flow_60s",
    "dispersion_scaled",
    "asset_btc",
    "asset_eth",
    "asset_sol",
    "asset_xrp",
    "asset_doge",
    "minutes_2_4",
    "minutes_4_8",
    "minutes_8_12",
)
FEATURE_SCHEMA_VERSION = 2
EVALUATION_REVISION = "causal-captured-asks-market-weighted-v2"
RESIDUAL_FEATURE_NAMES = tuple(
    name for name in FEATURE_NAMES if name != "market_probability"
)

ENSEMBLE_LOGISTIC_WEIGHTS = (0.0, 0.25, 0.5, 0.75, 0.9, 1.0)
EMPIRICAL_MIN_BIN_MARKETS = 30


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, float(value)))


def sigmoid(value):
    value = max(-35.0, min(35.0, float(value)))
    return 1.0 / (1.0 + math.exp(-value))


def logit(probability):
    probability = clamp(probability, 1e-6, 1.0 - 1e-6)
    return math.log(probability / (1.0 - probability))


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def minute_bucket(minutes):
    minutes = number(minutes)
    if minutes < 4:
        return "2-4"
    if minutes < 8:
        return "4-8"
    return "8-12"


def confidence_bucket(probability):
    """Bucket the model's selected-side confidence symmetrically around 50%."""
    confidence = max(clamp(probability), 1.0 - clamp(probability))
    if confidence < 0.55:
        return "50-55"
    if confidence < 0.60:
        return "55-60"
    if confidence < 0.65:
        return "60-65"
    if confidence < 0.70:
        return "65-70"
    return "70+"


def atomic_write_json(path, payload):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    last_error = None
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    raise last_error


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def candidate_feature_payload(candidate):
    model = candidate.get("model") or {}
    probability = candidate.get("probability") or {}
    micro = candidate.get("microstructure") or {}
    coinbase = micro.get("coinbase") or {}
    kraken = micro.get("kraken") or {}
    kalshi = candidate.get("kalshi_microstructure") or {}
    asset = str(candidate.get("asset") or "").upper()
    minutes = number(candidate.get("minutes_to_close") or probability.get("minutes_to_close"))
    market_prob = number(probability.get("market_implied_yes"), 50.0) / 100.0
    heuristic_prob = number(
        probability.get("pre_anchor_prob")
        if probability.get("pre_anchor_prob") is not None
        else candidate.get("model_prob_yes"),
        50.0,
    ) / 100.0
    values = {
        "minutes_fraction": clamp(minutes / 15.0),
        "target_distance_sigma": max(-5.0, min(5.0, number(probability.get("target_distance_sigma")))),
        "market_probability": clamp(market_prob),
        "heuristic_probability": clamp(heuristic_prob),
        "momentum_1m_scaled": max(-5.0, min(5.0, number(model.get("momentum_1m")) * 1000.0)),
        "momentum_5m_scaled": max(-5.0, min(5.0, number(model.get("momentum_5m")) * 1000.0)),
        "momentum_15m_scaled": max(-5.0, min(5.0, number(model.get("momentum_15m")) * 1000.0)),
        "momentum_60m_scaled": max(-5.0, min(5.0, number(model.get("momentum_60m")) * 1000.0)),
        "minute_vol_scaled": max(0.0, min(5.0, number(model.get("minute_vol")) * 1000.0)),
        "trend_bias": max(-5.0, min(5.0, number(model.get("trend_bias")))),
        "fakeout_risk": clamp(number(model.get("fakeout_risk"))),
        "underlying_flow": max(-1.0, min(1.0, number(micro.get("underlying_flow_score")))),
        "coinbase_book_imbalance": max(-1.0, min(1.0, number(coinbase.get("book_imbalance")))),
        "coinbase_trade_flow_60s": max(-1.0, min(1.0, number(coinbase.get("trade_flow_60s")))),
        "kraken_book_imbalance": max(-1.0, min(1.0, number(kraken.get("book_imbalance")))),
        "kraken_trade_flow_60s": max(-1.0, min(1.0, number(kraken.get("trade_flow_60s")))),
        "kalshi_book_imbalance": max(-1.0, min(1.0, number(kalshi.get("book_imbalance")))),
        "kalshi_trade_flow_60s": max(-1.0, min(1.0, number(kalshi.get("trade_flow_60s")))),
        "dispersion_scaled": max(0.0, min(5.0, number(micro.get("cross_exchange_dispersion_bps")) / 10.0)),
        "asset_btc": 1.0 if asset == "BTC" else 0.0,
        "asset_eth": 1.0 if asset == "ETH" else 0.0,
        "asset_sol": 1.0 if asset == "SOL" else 0.0,
        "asset_xrp": 1.0 if asset == "XRP" else 0.0,
        "asset_doge": 1.0 if asset == "DOGE" else 0.0,
        "minutes_2_4": 1.0 if minute_bucket(minutes) == "2-4" else 0.0,
        "minutes_4_8": 1.0 if minute_bucket(minutes) == "4-8" else 0.0,
        "minutes_8_12": 1.0 if minute_bucket(minutes) == "8-12" else 0.0,
    }
    return {name: round(number(values.get(name)), 8) for name in FEATURE_NAMES}


def candidate_feature_availability(candidate):
    """Keep missing inputs distinct from a genuinely neutral numeric zero."""
    model = candidate.get("model") or {}
    probability = candidate.get("probability") or {}
    micro = candidate.get("microstructure") or {}
    coinbase = micro.get("coinbase") or {}
    kraken = micro.get("kraken") or {}
    kalshi = candidate.get("kalshi_microstructure") or {}
    paths = {
        "target_distance_sigma": (probability, "target_distance_sigma"),
        "market_probability": (probability, "market_implied_yes"),
        "momentum_1m_scaled": (model, "momentum_1m"),
        "momentum_5m_scaled": (model, "momentum_5m"),
        "momentum_15m_scaled": (model, "momentum_15m"),
        "momentum_60m_scaled": (model, "momentum_60m"),
        "minute_vol_scaled": (model, "minute_vol"),
        "trend_bias": (model, "trend_bias"),
        "fakeout_risk": (model, "fakeout_risk"),
        "underlying_flow": (micro, "underlying_flow_score"),
        "coinbase_book_imbalance": (coinbase, "book_imbalance"),
        "coinbase_trade_flow_60s": (coinbase, "trade_flow_60s"),
        "kraken_book_imbalance": (kraken, "book_imbalance"),
        "kraken_trade_flow_60s": (kraken, "trade_flow_60s"),
        "kalshi_book_imbalance": (kalshi, "book_imbalance"),
        "kalshi_trade_flow_60s": (kalshi, "trade_flow_60s"),
        "dispersion_scaled": (micro, "cross_exchange_dispersion_bps"),
    }
    available = {
        name: bool(key in container and container.get(key) is not None)
        for name, (container, key) in paths.items()
    }
    available["heuristic_probability"] = any(
        key in probability and probability.get(key) is not None
        for key in ("heuristic_prob_before_ml", "pre_anchor_prob")
    )
    for name in FEATURE_NAMES:
        if name.startswith("asset_") or name.startswith("minutes_") or name == "minutes_fraction":
            available[name] = True
        else:
            available.setdefault(name, False)
    return available


def candidate_snapshot(candidate, now=None):
    now = now or datetime.now(timezone.utc)
    probability = candidate.get("probability") or {}
    minutes = number(candidate.get("minutes_to_close") or probability.get("minutes_to_close"))
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "snapshot_at": now.isoformat(timespec="seconds"),
        "ticker": candidate.get("ticker"),
        "event_ticker": candidate.get("event_ticker"),
        "asset": candidate.get("asset"),
        "close_time": candidate.get("close_time"),
        "minute_bucket": minute_bucket(minutes),
        "minute_index": int(max(0, math.floor(minutes))),
        "minutes_to_close": round(minutes, 4),
        "market_probability": round(number(probability.get("market_implied_yes"), 50.0) / 100.0, 6),
        "heuristic_probability": round(number(
            probability.get("heuristic_prob_before_ml")
            if probability.get("heuristic_prob_before_ml") is not None
            else candidate.get("model_prob_yes"),
            50.0,
        ) / 100.0, 6),
        "side": candidate.get("side"),
        "entry_price": round(number(candidate.get("entry_price")), 4),
        "yes_ask": candidate.get("yes_ask"),
        "no_ask": candidate.get("no_ask"),
        "fee_schedule": candidate.get("fee_schedule") or {},
        "quote_fetched_at": candidate.get("market_quote_fetched_at"),
        "edge": round(number(candidate.get("edge")), 4),
        "confidence": round(number(candidate.get("confidence")), 4),
        "floor_strike": candidate.get("floor_strike"),
        "cap_strike": candidate.get("cap_strike"),
        "market_kind": candidate.get("market_kind"),
        "expected_edge": candidate.get("expected_edge"),
        "edge_low": candidate.get("edge_low"),
        "probability_net_edge_positive": candidate.get("probability_net_edge_positive"),
        "exact_fee_cents": candidate.get("exact_fee_cents"),
        "expected_slippage_cents": candidate.get("expected_slippage_cents"),
        "break_even_probability": (
            number(candidate.get("entry_price"))
            + number(candidate.get("exact_fee_cents"))
            + number(candidate.get("expected_slippage_cents"))
        ),
        "probability_interval": {
            key: probability.get(key)
            for key in (
                "p_yes",
                "p_low",
                "p_high",
                "probability_sigma_pp",
                "interval_level",
                "interval_status",
                "uncertainty_components",
            )
            if probability.get(key) is not None
        },
        "data_quality": candidate.get("data_quality") or {},
        "execution_tier_pricing": candidate.get("execution_tier_pricing") or {},
        "decision": candidate.get("decision"),
        "skip_reasons": candidate.get("skip_reasons") or [],
        "settlement_proxy_spot": number(
            (candidate.get("microstructure") or {}).get("settlement_proxy_spot")
            or (candidate.get("model") or {}).get("spot")
        ),
        "market_regime_shadow": candidate.get("market_regime_shadow"),
        "market_regime_live": candidate.get("market_regime_live"),
        "features": candidate_feature_payload(candidate),
        "feature_availability": candidate_feature_availability(candidate),
        "research_feature_schema_version": RESEARCH_FEATURE_SCHEMA_VERSION,
        "research_features": research_feature_payload(candidate, scanned_at=now),
    }


def record_pending_snapshots(candidates, pending_file, max_pending=50000, now=None):
    pending_file = Path(pending_file)
    state = read_json(pending_file, {"version": 1, "snapshots": {}})
    snapshots = state.setdefault("snapshots", {})
    added = 0
    for candidate in candidates or []:
        if not candidate.get("is_15m_market") or not candidate.get("ticker"):
            continue
        snapshot = candidate_snapshot(candidate, now=now)
        key = f"{snapshot['ticker']}|{snapshot['minute_index']}"
        if key not in snapshots:
            added += 1
        snapshots[key] = snapshot
    if len(snapshots) > int(max_pending):
        ordered = sorted(snapshots.items(), key=lambda item: str(item[1].get("snapshot_at") or ""))
        snapshots = dict(ordered[-int(max_pending):])
        state["snapshots"] = snapshots
    state["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_write_json(pending_file, state)
    return {"added": added, "pending": len(snapshots)}


def fetch_market_settlement(base_url, ticker, timeout=5):
    response = requests.get(
        f"{base_url}/markets/{quote(str(ticker), safe='')}",
        timeout=timeout,
        headers={"User-Agent": "polymaker-crypto-learning/1.0"},
    )
    response.raise_for_status()
    market = response.json().get("market") or {}
    result = str(market.get("result") or "").lower()
    if str(market.get("status") or "").lower() != "finalized" or result not in {"yes", "no"}:
        return {}
    settlement_value = None
    settlement_field = None
    for key in (
        "expiration_value",
        "settlement_value",
        "settlement_value_dollars",
        "settlement_price",
        "settlement_price_dollars",
    ):
        value = market.get(key)
        if value in (None, ""):
            continue
        try:
            settlement_value = float(value)
            settlement_field = key
            break
        except (TypeError, ValueError):
            continue
    return {
        "result": result,
        "status": "finalized",
        "settlement_value": settlement_value,
        "settlement_value_field": settlement_field,
        "settlement_ts": market.get("settlement_ts") or market.get("settled_time"),
    }


def fetch_market_result(base_url, ticker, timeout=5):
    return fetch_market_settlement(base_url, ticker, timeout=timeout).get("result", "")


def settlement_margin(snapshot, official_value):
    """Return a continuous signed distance from the contract's settlement boundary."""
    value = number(official_value)
    if value <= 0:
        return None
    floor = snapshot.get("floor_strike")
    cap = snapshot.get("cap_strike")
    floor = number(floor) if floor not in (None, "") else None
    cap = number(cap) if cap not in (None, "") else None
    kind = str(snapshot.get("market_kind") or "").lower()
    if kind == "above" and floor is not None:
        return value - floor
    if kind == "below" and cap is not None:
        return cap - value
    if kind == "range" and floor is not None and cap is not None:
        return min(value - floor, cap - value)
    return None


def resolve_pending_snapshots(
    base_url,
    pending_file,
    dataset_file,
    now=None,
    settlement_grace_minutes=2.0,
    max_markets_per_run=20,
    result_fetcher=None,
    proxy_window_provider=None,
):
    now = now or datetime.now(timezone.utc)
    pending_file = Path(pending_file)
    dataset_file = Path(dataset_file)
    state = read_json(pending_file, {"version": 1, "snapshots": {}})
    snapshots = state.setdefault("snapshots", {})
    due_by_ticker = defaultdict(list)
    for key, snapshot in snapshots.items():
        close_time = parse_time(snapshot.get("close_time"))
        if close_time and now >= close_time + timedelta(minutes=float(settlement_grace_minutes)):
            due_by_ticker[str(snapshot.get("ticker") or "")].append((key, snapshot))
    resolved_rows = []
    resolved_markets = 0
    settled_markets = []
    fetcher = result_fetcher or (
        lambda ticker: fetch_market_settlement(base_url, ticker)
    )
    for ticker in sorted(due_by_ticker)[:max(0, int(max_markets_per_run))]:
        if not ticker:
            continue
        try:
            settlement = fetcher(ticker)
        except Exception:
            continue
        if isinstance(settlement, str):
            settlement = {"result": settlement}
        settlement = settlement if isinstance(settlement, dict) else {}
        result = str(settlement.get("result") or "").lower()
        if result not in {"yes", "no"}:
            continue
        resolved_markets += 1
        label = 1 if result == "yes" else 0
        settlement_summary = None
        for key, snapshot in due_by_ticker[ticker]:
            proxy_window = {}
            if proxy_window_provider:
                try:
                    proxy_window = proxy_window_provider(
                        snapshot.get("asset"),
                        snapshot.get("close_time"),
                    ) or {}
                except Exception:
                    proxy_window = {}
            official_value = number(settlement.get("settlement_value"))
            proxy_average = number(proxy_window.get("average"))
            residual_bps = (
                (official_value / proxy_average - 1.0) * 10000.0
                if official_value > 0 and proxy_average > 0
                else None
            )
            margin = settlement_margin(snapshot, official_value)
            margin_bps = (
                margin / official_value * 10000.0
                if margin is not None and official_value > 0
                else None
            )
            row = {
                **snapshot,
                "label_yes": label,
                "market_result": result,
                "resolved_at": now.isoformat(timespec="seconds"),
                "official_settlement_value": official_value or None,
                "official_settlement_value_field": settlement.get("settlement_value_field"),
                "official_settlement_ts": settlement.get("settlement_ts"),
                "settlement_proxy_window": proxy_window,
                "settlement_proxy_average": proxy_average or None,
                "settlement_basis_residual_bps": round(residual_bps, 6) if residual_bps is not None else None,
                "settlement_margin": round(margin, 10) if margin is not None else None,
                "settlement_margin_bps": round(margin_bps, 6) if margin_bps is not None else None,
            }
            resolved_rows.append(row)
            if settlement_summary is None:
                settlement_summary = {
                    "ticker": ticker,
                    "result": result,
                    "settlement_value": official_value or None,
                    "settlement_value_field": settlement.get("settlement_value_field"),
                    "settlement_ts": settlement.get("settlement_ts"),
                    "settlement_margin": row.get("settlement_margin"),
                    "settlement_margin_bps": row.get("settlement_margin_bps"),
                }
            snapshots.pop(key, None)
        if settlement_summary:
            settled_markets.append(settlement_summary)
    if resolved_rows:
        with dataset_file.open("a", encoding="utf-8") as handle:
            for row in resolved_rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    state["updated_at"] = now.isoformat(timespec="seconds")
    atomic_write_json(pending_file, state)
    return {
        "resolved_markets": resolved_markets,
        "resolved_snapshots": len(resolved_rows),
        "pending": len(snapshots),
        "settled_markets": settled_markets,
    }


def load_dataset(path, max_rows=100000):
    path = Path(path)
    rows = {}
    for row in iter_jsonl(jsonl_sources(path)):
        if (
                row.get("ticker")
                and row.get("label_yes") in {0, 1}
                and row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION
                and isinstance(row.get("features"), dict)
        ):
            key = (row["ticker"], row.get("snapshot_at"), row.get("feature_schema_version"))
            rows[key] = row
    ordered = sorted(rows.values(), key=lambda row: (str(row.get("close_time") or ""), str(row.get("snapshot_at") or "")))
    return ordered[-max(1, int(max_rows)):]


def vector(row, feature_names=None):
    feature_names = tuple(feature_names or FEATURE_NAMES)
    features = row.get("features") or {}
    return [number(features.get(name)) for name in feature_names]


def market_sample_weights(rows):
    """Give every independent market one total unit of training weight."""
    counts = defaultdict(int)
    for row in rows:
        counts[str(row.get("ticker") or "")] += 1
    return [
        1.0 / max(1, counts[str(row.get("ticker") or "")])
        for row in rows
    ]


def standardizer(rows, feature_names=None, sample_weights=None):
    feature_names = tuple(feature_names or FEATURE_NAMES)
    vectors = [vector(row, feature_names) for row in rows]
    if not vectors:
        return [0.0] * len(feature_names), [1.0] * len(feature_names)
    sample_weights = list(sample_weights or market_sample_weights(rows))
    total_weight = max(1e-12, sum(sample_weights))
    means = [
        sum(weight * values[index] for weight, values in zip(sample_weights, vectors))
        / total_weight
        for index in range(len(feature_names))
    ]
    stds = []
    for index, mean in enumerate(means):
        variance = sum(
            weight * (values[index] - mean) ** 2
            for weight, values in zip(sample_weights, vectors)
        ) / total_weight
        stds.append(max(1e-6, math.sqrt(variance)))
    return means, stds


def scale_vector(values, means, stds):
    return [(number(value) - means[index]) / stds[index] for index, value in enumerate(values)]


def train_logistic(rows, iterations=350, learning_rate=0.08, l2=0.02):
    feature_names = RESIDUAL_FEATURE_NAMES
    sample_weights = market_sample_weights(rows)
    means, stds = standardizer(
        rows,
        feature_names=feature_names,
        sample_weights=sample_weights,
    )
    x_rows = [
        scale_vector(vector(row, feature_names), means, stds)
        for row in rows
    ]
    labels = [int(row["label_yes"]) for row in rows]
    market_offsets = [
        logit(number(row.get("market_probability"), 0.5))
        for row in rows
    ]
    weights = [0.0] * len(feature_names)
    intercept = 0.0
    for iteration in range(max(1, int(iterations))):
        grad_w = [0.0] * len(weights)
        grad_b = 0.0
        for values, label, market_offset, sample_weight in zip(
            x_rows,
            labels,
            market_offsets,
            sample_weights,
        ):
            prediction = sigmoid(
                market_offset
                + intercept
                + sum(weight * value for weight, value in zip(weights, values))
            )
            error = prediction - label
            grad_b += sample_weight * error
            for index, value in enumerate(values):
                grad_w[index] += sample_weight * error * value
        count = max(1e-12, sum(sample_weights))
        step = learning_rate / math.sqrt(1.0 + iteration / 40.0)
        intercept -= step * grad_b / count
        for index in range(len(weights)):
            weights[index] -= step * (grad_w[index] / count + l2 * weights[index])
    return {
        "type": "market_residual_logistic",
        "features": list(feature_names),
        "means": means,
        "stds": stds,
        "weights": weights,
        "intercept": intercept,
        "sample_weighting": "inverse_snapshots_per_market",
        "effective_markets": round(sum(sample_weights), 4),
    }


def predict_logistic(model, row):
    feature_names = model.get("features") or FEATURE_NAMES
    values = scale_vector(vector(row, feature_names), model["means"], model["stds"])
    market_offset = (
        logit(number(row.get("market_probability"), 0.5))
        if model.get("type") == "market_residual_logistic"
        else 0.0
    )
    return sigmoid(market_offset + number(model.get("intercept")) + sum(
        number(weight) * value
        for weight, value in zip(model.get("weights") or [], values)
    ))


def quantiles(values):
    ordered = sorted(set(number(value) for value in values))
    if len(ordered) < 2:
        return []
    return sorted({
        ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]
        for fraction in (0.1, 0.25, 0.5, 0.75, 0.9)
    })


def train_boosted_stumps(rows, estimators=32, learning_rate=0.12):
    feature_names = RESIDUAL_FEATURE_NAMES
    x_rows = [vector(row, feature_names) for row in rows]
    labels = [int(row["label_yes"]) for row in rows]
    sample_weights = market_sample_weights(rows)
    raw_scores = [
        logit(number(row.get("market_probability"), 0.5))
        for row in rows
    ]
    stumps = []
    thresholds = [
        quantiles([values[index] for values in x_rows])
        for index in range(len(feature_names))
    ]
    for _iteration in range(max(1, int(estimators))):
        probabilities = [sigmoid(score) for score in raw_scores]
        residuals = [label - probability for label, probability in zip(labels, probabilities)]
        best = None
        for feature_index, feature_thresholds in enumerate(thresholds):
            for threshold in feature_thresholds:
                left = [index for index, values in enumerate(x_rows) if values[feature_index] <= threshold]
                right = [index for index, values in enumerate(x_rows) if values[feature_index] > threshold]
                if len(left) < 5 or len(right) < 5:
                    continue
                left_weight = sum(sample_weights[index] for index in left)
                right_weight = sum(sample_weights[index] for index in right)
                if left_weight <= 0 or right_weight <= 0:
                    continue
                left_value = sum(
                    sample_weights[index] * residuals[index]
                    for index in left
                ) / left_weight
                right_value = sum(
                    sample_weights[index] * residuals[index]
                    for index in right
                ) / right_weight
                error = (
                    sum(
                        sample_weights[index]
                        * (residuals[index] - left_value) ** 2
                        for index in left
                    )
                    + sum(
                        sample_weights[index]
                        * (residuals[index] - right_value) ** 2
                        for index in right
                    )
                )
                if best is None or error < best["error"]:
                    best = {
                        "feature_index": feature_index,
                        "threshold": threshold,
                        "left": max(-1.0, min(1.0, left_value)),
                        "right": max(-1.0, min(1.0, right_value)),
                        "error": error,
                    }
        if not best:
            break
        stump = {key: value for key, value in best.items() if key != "error"}
        stumps.append(stump)
        for index, values in enumerate(x_rows):
            leaf = stump["left"] if values[stump["feature_index"]] <= stump["threshold"] else stump["right"]
            raw_scores[index] += learning_rate * leaf
    return {
        "type": "market_residual_boosted_stumps",
        "features": list(feature_names),
        "learning_rate": learning_rate,
        "stumps": stumps,
        "sample_weighting": "inverse_snapshots_per_market",
        "effective_markets": round(sum(sample_weights), 4),
    }


def predict_boosted(model, row):
    feature_names = model.get("features") or FEATURE_NAMES
    values = vector(row, feature_names)
    score = (
        logit(number(row.get("market_probability"), 0.5))
        if model.get("type") == "market_residual_boosted_stumps"
        else number(model.get("base_logit"))
    )
    learning_rate = number(model.get("learning_rate"), 0.12)
    for stump in model.get("stumps") or []:
        index = int(stump.get("feature_index") or 0)
        leaf = stump.get("left") if values[index] <= number(stump.get("threshold")) else stump.get("right")
        score += learning_rate * number(leaf)
    return sigmoid(score)


def fit_platt(
    probabilities,
    labels,
    iterations=250,
    learning_rate=0.05,
    sample_weights=None,
):
    if not probabilities or len(set(labels)) < 2:
        return {"slope": 1.0, "intercept": 0.0, "samples": len(probabilities)}
    sample_weights = list(sample_weights or [1.0] * len(probabilities))
    slope, intercept = 1.0, 0.0
    inputs = [logit(probability) for probability in probabilities]
    for iteration in range(max(1, int(iterations))):
        grad_slope = 0.0
        grad_intercept = 0.0
        for value, label, sample_weight in zip(inputs, labels, sample_weights):
            error = sigmoid(slope * value + intercept) - label
            grad_slope += sample_weight * error * value
            grad_intercept += sample_weight * error
        step = learning_rate / math.sqrt(1.0 + iteration / 50.0)
        count = max(1e-12, sum(sample_weights))
        slope -= step * grad_slope / count
        intercept -= step * grad_intercept / count
    return {
        "slope": slope,
        "intercept": intercept,
        "samples": max(1, int(round(sum(sample_weights)))),
        "raw_snapshots": len(probabilities),
        "sample_weighting": "inverse_snapshots_per_market",
    }


def apply_platt(probability, calibrator):
    return sigmoid(
        number(calibrator.get("slope"), 1.0) * logit(probability)
        + number(calibrator.get("intercept"))
    )


def centered_calibrator(calibrator):
    """Calibrate probability sharpness without learning a fragile base-rate shift."""
    return {
        "slope": max(0.25, min(2.0, number((calibrator or {}).get("slope"), 1.0))),
        "intercept": 0.0,
        "samples": int(number((calibrator or {}).get("samples"))),
        "mode": "centered_slope_only",
    }


def regularized_calibrator(calibrator):
    """Retain a shrunk base-rate correction instead of forcing it to zero."""
    row = calibrator or {}
    samples = max(0, int(number(row.get("samples"))))
    shrink = samples / (samples + 100.0)
    return {
        "slope": max(0.25, min(2.0, 1.0 + (number(row.get("slope"), 1.0) - 1.0) * shrink)),
        "intercept": max(-0.75, min(0.75, number(row.get("intercept")) * shrink)),
        "samples": samples,
        "mode": "regularized_platt",
    }


def blend_probability(logistic_probability, boosted_probability, weights=None):
    weights = weights or {}
    logistic_weight = clamp(weights.get("logistic", 0.5))
    boosted_weight = 1.0 - logistic_weight
    return (
        logistic_weight * float(logistic_probability)
        + boosted_weight * float(boosted_probability)
    )


def metrics(probabilities, labels):
    if not probabilities:
        return {"samples": 0, "brier": None, "log_loss": None}
    clipped = [clamp(probability, 1e-6, 1.0 - 1e-6) for probability in probabilities]
    brier = sum((probability - label) ** 2 for probability, label in zip(clipped, labels)) / len(labels)
    log_loss = -sum(
        label * math.log(probability) + (1 - label) * math.log(1.0 - probability)
        for probability, label in zip(clipped, labels)
    ) / len(labels)
    return {"samples": len(labels), "brier": round(brier, 6), "log_loss": round(log_loss, 6)}


def market_weighted_metrics(probabilities, labels, market_keys):
    grouped = defaultdict(list)
    for probability, label, key in zip(probabilities, labels, market_keys):
        grouped[str(key)].append((clamp(probability), int(label)))
    losses, log_losses, market_probabilities, market_labels = [], [], [], []
    for values in grouped.values():
        market_probabilities.append(sum(value[0] for value in values) / len(values))
        market_labels.append(sum(value[1] for value in values) / len(values))
        scored = metrics([v[0] for v in values], [v[1] for v in values])
        losses.append(scored["brier"])
        log_losses.append(scored["log_loss"])
    count = len(grouped)
    predicted = sum(market_probabilities) / count if count else None
    actual = sum(market_labels) / count if count else None
    return {
        "samples": count, "markets": count,
        "brier": round(sum(losses) / count, 6) if count else None,
        "log_loss": round(sum(log_losses) / count, 6) if count else None,
        "average_probability": predicted, "actual_rate": actual,
        "calibration_gap": abs(predicted - actual) if count else None,
        "method": "equal_market_mean_of_snapshot_proper_losses_v2",
    }


def causal_policy_decisions(probabilities, rows, minimum_confidence):
    """First positive-value observable decision per market, never hindsight max."""
    selected = {}
    for probability, row in sorted(zip(probabilities, rows), key=lambda pair: str(pair[1].get("snapshot_at") or "")):
        key = str(row.get("ticker") or "")
        if not key or key in selected:
            continue
        probability = clamp(probability)
        side = "yes" if probability >= .5 else "no"
        confidence = max(probability, 1-probability)
        if confidence < minimum_confidence:
            continue
        captured = parse_time(row.get("snapshot_at"))
        close = parse_time(row.get("close_time"))
        if not captured or not close or captured >= close:
            continue
        raw_price = row.get(side + "_ask")
        if raw_price is None and str(row.get("side") or "").lower() == side:
            raw_price = row.get("entry_price")
        # A complement of an ask is a bid, not an executable opposite ask.
        if raw_price is None or not 0 < number(raw_price) < 100:
            continue
        price = number(raw_price) / 100
        schedule = row.get("fee_schedule") or {}
        if schedule.get("authoritative"):
            fee = kalshi_order_fee(raw_price, 1, schedule=schedule)["conservative_fee_dollars"]
        elif str(row.get("side") or "").lower() == side and row.get("exact_fee_cents") is not None:
            fee = math.ceil((price + number(row["exact_fee_cents"]) / 100) * 100 - 1e-9) / 100 - price
        else:
            continue
        slippage = max(.01, number(row.get("expected_slippage_cents")) / 100)
        cost = price + fee + slippage
        if confidence <= cost:
            continue
        won = (side == "yes") == bool(row.get("label_yes"))
        selected[key] = {"profit": float(won)-cost, "cost": cost,
                         "score": confidence-cost, "won": won, "confidence": confidence,
                         "snapshot_at": row.get("snapshot_at"), "side": side}
    return selected


def after_fee_policy_metrics(probabilities, rows, minimum_confidence=0.55):
    """Causal captured-ask diagnostic; still not exchange-confirmed execution."""
    best_by_market = causal_policy_decisions(probabilities, rows, minimum_confidence)
    profit = sum(row["profit"] for row in best_by_market.values())
    cost = sum(row["cost"] for row in best_by_market.values())
    market_count = len(best_by_market)
    return {
        "markets": market_count,
        "entry_policy": "first_eligible_captured_ask_v2",
        "execution_confirmed": False,
        "total_profit_per_one_contract_decision": round(profit, 6),
        "profit_per_contract": round(profit / market_count, 6) if market_count else None,
        "roi": round(profit / cost, 6) if cost > 0 else None,
    }


def selected_side_calibration(
    probabilities,
    labels,
    low=0.55,
    high=0.65,
    market_keys=None,
):
    """Measure selected-side calibration with each settled market weighted once."""
    grouped = defaultdict(list)
    keys = market_keys or [str(index) for index in range(len(probabilities))]
    selected_snapshots = 0
    for probability, label, market_key in zip(probabilities, labels, keys):
        probability = clamp(probability)
        confidence = max(probability, 1.0 - probability)
        if float(low) <= confidence < float(high):
            predicted_yes = probability >= 0.5
            correct = int(bool(label) == predicted_yes)
            grouped[str(market_key)].append((confidence, correct))
            selected_snapshots += 1
    if not grouped:
        return {
            "samples": 0,
            "markets": 0,
            "average_confidence": None,
            "accuracy": None,
            "calibration_gap": None,
        }
    market_rows = [
        (
            sum(confidence for confidence, _correct in values) / len(values),
            sum(correct for _confidence, correct in values) / len(values),
        )
        for values in grouped.values()
    ]
    average_confidence = sum(confidence for confidence, _correct in market_rows) / len(market_rows)
    accuracy = sum(correct for _confidence, correct in market_rows) / len(market_rows)
    return {
        "samples": selected_snapshots,
        "markets": len(market_rows),
        "average_confidence": round(average_confidence, 6),
        "accuracy": round(accuracy, 6),
        "calibration_gap": round(abs(average_confidence - accuracy), 6),
    }


def _market_probability_rows(probabilities, labels, rows):
    grouped = defaultdict(list)
    for probability, label, row in zip(probabilities, labels, rows):
        grouped[str(row.get("ticker") or "")].append(
            (clamp(probability), int(label), row)
        )
    result = []
    for ticker, values in grouped.items():
        probability = sum(value[0] for value in values) / len(values)
        label = round(sum(value[1] for value in values) / len(values))
        result.append({
            "ticker": ticker,
            "probability": probability,
            "label": label,
            "row": values[len(values) // 2][2],
        })
    return result


def fit_empirical_probability_bins(
    probabilities,
    labels,
    rows,
    *,
    bin_width=0.10,
    prior_strength=100.0,
    interval_z=1.6448536269514722,
    minimum_bin_markets=EMPIRICAL_MIN_BIN_MARKETS,
):
    """Fit a shrunk win-rate calibration interval with one vote per market."""
    market_rows = _market_probability_rows(probabilities, labels, rows)
    grouped = defaultdict(list)
    for row in market_rows:
        index = min(
            int(math.ceil(1.0 / bin_width)) - 1,
            max(0, int(row["probability"] / bin_width)),
        )
        grouped[index].append(row)
    bins = {}
    for index, values in grouped.items():
        count = len(values)
        predicted = sum(row["probability"] for row in values) / count
        wins = sum(row["label"] for row in values)
        alpha = wins + prior_strength * predicted
        beta = count - wins + prior_strength * (1.0 - predicted)
        total = alpha + beta
        posterior = alpha / total
        variance = alpha * beta / max(1e-12, total * total * (total + 1.0))
        half_width = interval_z * math.sqrt(max(0.0, variance))
        observed = wins / count
        bins[str(index)] = {
            "index": index,
            "lower_probability": round(index * bin_width, 6),
            "upper_probability": round(min(1.0, (index + 1) * bin_width), 6),
            "markets": count,
            "wins": wins,
            "average_predicted_probability": round(predicted, 6),
            "observed_win_rate": round(observed, 6),
            "posterior_probability": round(clamp(posterior), 6),
            "probability_low": round(clamp(posterior - half_width), 6),
            "probability_high": round(clamp(posterior + half_width), 6),
            "calibration_gap": round(abs(observed - predicted), 6),
        }
    return {
        "method": "market_weighted_beta_binomial_v2",
        "interval_level": 0.90,
        "bin_width": bin_width,
        "prior_strength": prior_strength,
        "minimum_bin_markets": max(1, int(minimum_bin_markets)),
        "markets": len(market_rows),
        "bins": bins,
    }


def fit_empirical_probability_calibration(probabilities, labels, rows):
    calibration = {
        "global": fit_empirical_probability_bins(probabilities, labels, rows),
        "subgroups": {},
    }
    grouped = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[f"{row.get('asset')}|{row.get('minute_bucket')}"].append(index)
    for key, indexes in grouped.items():
        market_count = len({str(rows[index].get("ticker") or "") for index in indexes})
        if market_count < 30:
            continue
        calibration["subgroups"][key] = fit_empirical_probability_bins(
            [probabilities[index] for index in indexes],
            [labels[index] for index in indexes],
            [rows[index] for index in indexes],
        )
    return calibration


def empirical_probability_interval(calibration, row, probability):
    calibration = calibration or {}
    subgroup_key = f"{row.get('asset')}|{row.get('minute_bucket')}"
    sources = []
    subgroup = (calibration.get("subgroups") or {}).get(subgroup_key)
    if subgroup:
        sources.append((subgroup_key, subgroup))
    global_source = calibration.get("global") or {}
    if global_source:
        sources.append(("global", global_source))
    selected = None
    selected_source = None
    scope = subgroup_key if subgroup else "global"
    index = None
    observed_markets = 0
    minimum_markets = EMPIRICAL_MIN_BIN_MARKETS
    for candidate_scope, source in sources:
        bin_width = number(source.get("bin_width"), 0.10)
        maximum_index = max(0, int(math.ceil(1.0 / max(0.01, bin_width))) - 1)
        candidate_index = min(
            maximum_index,
            max(0, int(clamp(probability) / bin_width)),
        )
        candidate = (source.get("bins") or {}).get(str(candidate_index))
        candidate_markets = int(number((candidate or {}).get("markets")))
        candidate_minimum = max(
            1,
            int(number(source.get("minimum_bin_markets"), EMPIRICAL_MIN_BIN_MARKETS)),
        )
        observed_markets = max(observed_markets, candidate_markets)
        minimum_markets = candidate_minimum
        if candidate and candidate_markets >= candidate_minimum:
            selected = candidate
            selected_source = source
            scope = candidate_scope
            index = candidate_index
            break
    if not selected:
        raw_probability = clamp(probability)
        return {
            "available": False,
            "probability": raw_probability,
            "low": raw_probability,
            "high": raw_probability,
            "scope": scope,
            "markets": observed_markets,
            "minimum_markets": minimum_markets,
            "reason": "insufficient_exact_bin_markets",
        }
    return {
        "available": True,
        "probability": clamp(selected.get("posterior_probability")),
        "low": clamp(selected.get("probability_low")),
        "high": clamp(selected.get("probability_high")),
        "scope": scope,
        "markets": int(number(selected.get("markets"))),
        "minimum_markets": max(
            1,
            int(number(selected_source.get("minimum_bin_markets"), EMPIRICAL_MIN_BIN_MARKETS)),
        ),
        "bin": int(number(selected.get("index", index))),
        "method": selected_source.get("method"),
        "interval_level": selected_source.get("interval_level", 0.90),
        "calibration_gap": number(selected.get("calibration_gap")),
    }


def evaluate_empirical_probability_intervals(probabilities, labels, rows, calibration):
    grouped = defaultdict(list)
    unavailable = 0
    for market_row in _market_probability_rows(probabilities, labels, rows):
        interval = empirical_probability_interval(
            calibration,
            market_row["row"],
            market_row["probability"],
        )
        if not interval.get("available"):
            unavailable += 1
            continue
        key = f"{interval.get('scope')}|{interval.get('bin')}"
        grouped[key].append((interval, int(market_row["label"])))
    evaluated = []
    for key, values in grouped.items():
        count = len(values)
        observed = sum(label for _interval, label in values) / count
        low = sum(interval["low"] for interval, _label in values) / count
        high = sum(interval["high"] for interval, _label in values) / count
        predicted = sum(interval["probability"] for interval, _label in values) / count
        evaluated.append({
            "key": key,
            "markets": count,
            "observed_win_rate": observed,
            "average_probability": predicted,
            "average_low": low,
            "average_high": high,
            "covered": low <= observed <= high,
            "calibration_gap": abs(observed - predicted),
        })
    total = sum(row["markets"] for row in evaluated)
    covered = sum(row["markets"] for row in evaluated if row["covered"])
    return {
        "method": "market_weighted_bin_rate_coverage_v1",
        "target_coverage": 0.90,
        "markets": total,
        "unavailable_markets": unavailable,
        "available_fraction": total / (total + unavailable) if total + unavailable else 0.0,
        "bins": len(evaluated),
        "covered_markets": covered,
        "coverage": round(covered / total, 6) if total else None,
        "maximum_calibration_gap": round(
            max((row["calibration_gap"] for row in evaluated), default=0.0),
            6,
        ),
        "details": evaluated,
    }


def probability_tier_policy_metrics(probabilities, rows):
    thresholds = {"1": 0.55, "2": 0.62, "3": 0.68, "4": 0.74, "5": 0.80}
    result = {}
    for tier, threshold in thresholds.items():
        best_by_market = causal_policy_decisions(probabilities, rows, threshold)
        values = list(best_by_market.values())
        profits = [row["profit"] for row in values]
        count = len(values)
        mean_profit = sum(profits) / count if count else 0.0
        variance = (
            sum((profit - mean_profit) ** 2 for profit in profits) / max(1, count - 1)
            if count > 1
            else 0.0
        )
        standard_error = math.sqrt(variance / count) if count else 0.0
        profit_low = mean_profit - 1.6448536269514722 * standard_error
        stake = sum(row["cost"] for row in values)
        profit = sum(profits)
        average_confidence = (
            sum(row["confidence"] for row in values) / count if count else None
        )
        accuracy = sum(1 for row in values if row["won"]) / count if count else None
        result[tier] = {
            "minimum_probability": threshold,
            "markets": count,
            "wins": sum(1 for row in values if row["won"]),
            "losses": sum(1 for row in values if not row["won"]),
            "profit_per_contract": round(mean_profit, 6) if count else None,
            "profit_per_contract_low_90": round(profit_low, 6) if count else None,
            "roi": round(profit / stake, 6) if stake else None,
            "average_confidence": round(average_confidence, 6) if count else None,
            "accuracy": round(accuracy, 6) if count else None,
            "calibration_gap": (
                round(abs(average_confidence - accuracy), 6) if count else None
            ),
        }
    return result


def conditional_reliability(probabilities, rows):
    """Expose calibration by the conditions that matter to short-horizon bets."""
    grouped = defaultdict(list)
    for probability, row in zip(probabilities, rows):
        quality = number((row.get("data_quality") or {}).get("score"), -1.0)
        quality_band = (
            "quality_missing" if quality < 0
            else "quality_<80" if quality < 0.80
            else "quality_80_90" if quality < 0.90
            else "quality_90+"
        )
        price = number(row.get("entry_price"), -1.0)
        price_band = (
            "price_missing" if price < 0
            else "price_<40" if price < 40
            else "price_40_55" if price < 55
            else "price_55_70" if price < 70
            else "price_70+"
        )
        for dimension, label in (
            ("asset", str(row.get("asset") or "unknown")),
            ("minute_bucket", str(row.get("minute_bucket") or "unknown")),
            ("price_band", price_band),
            ("data_quality", quality_band),
        ):
            grouped[(dimension, label)].append((probability, row))
    result = defaultdict(list)
    for (dimension, label), values in grouped.items():
        market_keys = [str(row.get("ticker") or "") for _probability, row in values]
        markets = len(set(market_keys))
        if markets < 5:
            continue
        probabilities_group = [probability for probability, _row in values]
        labels = [int(row.get("label_yes")) for _probability, row in values]
        weighted = market_weighted_metrics(
            probabilities_group,
            labels,
            market_keys,
        )
        result[dimension].append({
            "label": label,
            "markets": markets,
            "rows": len(values),
            "average_probability": weighted.get("average_probability"),
            "actual_rate": weighted.get("actual_rate"),
            "calibration_gap": weighted.get("calibration_gap"),
            "brier": weighted.get("brier"),
            "log_loss": weighted.get("log_loss"),
        })
    return {
        key: sorted(values, key=lambda row: row["label"])
        for key, values in result.items()
    }


def temporal_stability_windows(probabilities, rows, windows=3):
    """Check that held-out performance is not concentrated in one date slice."""
    markets = defaultdict(list)
    for probability, row in zip(probabilities, rows):
        markets[str(row.get("ticker") or "")].append((probability, row))
    ordered = sorted(
        markets.items(),
        key=lambda item: str(item[1][0][1].get("close_time") or item[0]),
    )
    window_count = min(max(1, int(windows)), len(ordered))
    if not ordered:
        return {"qualified": False, "windows": []}
    slices = []
    for index in range(window_count):
        start = int(index * len(ordered) / window_count)
        stop = int((index + 1) * len(ordered) / window_count)
        selected = ordered[start:stop]
        flat = [item for _ticker, values in selected for item in values]
        if not flat:
            continue
        model_probabilities = [probability for probability, _row in flat]
        labels = [int(row["label_yes"]) for _probability, row in flat]
        keys = [str(row.get("ticker") or "") for _probability, row in flat]
        market_probabilities = [number(row.get("market_probability"), 0.5) for _probability, row in flat]
        model_metrics = market_weighted_metrics(model_probabilities, labels, keys)
        market_metrics = market_weighted_metrics(market_probabilities, labels, keys)
        slices.append({
            "window": index + 1,
            "markets": len(selected),
            "first_close": selected[0][1][0][1].get("close_time"),
            "last_close": selected[-1][1][0][1].get("close_time"),
            "model": model_metrics,
            "market": market_metrics,
            "brier_improvement": round(
                number(market_metrics.get("brier"), 1.0)
                - number(model_metrics.get("brier"), 1.0),
                6,
            ),
            "log_loss_improvement": round(
                number(market_metrics.get("log_loss"), 99.0)
                - number(model_metrics.get("log_loss"), 99.0),
                6,
            ),
        })
    qualified = bool(
        len(slices) >= min(3, window_count)
        and all(row["markets"] >= 5 for row in slices)
        and all(row["brier_improvement"] > 0 for row in slices)
        and all(row["log_loss_improvement"] > 0 for row in slices)
    )
    return {"qualified": qualified, "windows": slices}


def feature_drift_summary(train_rows, test_rows):
    """Report standardized feature drift using one total weight per market."""
    if not train_rows or not test_rows:
        return {"available": False, "maximum_standardized_mean_shift": None, "features": []}
    train_weights = market_sample_weights(train_rows)
    test_weights = market_sample_weights(test_rows)
    train_means, train_stds = standardizer(
        train_rows,
        feature_names=RESIDUAL_FEATURE_NAMES,
        sample_weights=train_weights,
    )
    test_means, _test_stds = standardizer(
        test_rows,
        feature_names=RESIDUAL_FEATURE_NAMES,
        sample_weights=test_weights,
    )
    rows = []
    for name, train_mean, test_mean, train_std in zip(
        RESIDUAL_FEATURE_NAMES,
        train_means,
        test_means,
        train_stds,
    ):
        shift = abs(test_mean - train_mean) / max(1e-6, train_std)
        rows.append({
            "feature": name,
            "train_mean": round(train_mean, 6),
            "test_mean": round(test_mean, 6),
            "standardized_mean_shift": round(shift, 6),
        })
    rows.sort(key=lambda row: row["standardized_mean_shift"], reverse=True)
    ordered_shifts = sorted(row["standardized_mean_shift"] for row in rows)
    percentile_90 = (
        ordered_shifts[min(len(ordered_shifts) - 1, int(0.90 * (len(ordered_shifts) - 1)))]
        if ordered_shifts
        else 0.0
    )
    maximum_shift = rows[0]["standardized_mean_shift"] if rows else 0.0
    return {
        "available": True,
        "maximum_standardized_mean_shift": maximum_shift,
        "p90_standardized_mean_shift": round(percentile_90, 6),
        "features": rows[:12],
        "p90_threshold": 1.5,
        "maximum_threshold": 3.0,
        "qualified": bool(
            rows
            and percentile_90 <= 1.5
            and maximum_shift <= 3.0
        ),
    }


def research_feature_readiness(rows):
    research = [
        row for row in rows
        if row.get("research_feature_schema_version") == RESEARCH_FEATURE_SCHEMA_VERSION
        and isinstance((row.get("research_features") or {}).get("values"), dict)
    ]
    markets = {str(row.get("ticker") or "") for row in research}
    presence = defaultdict(list)
    for row in research:
        for name, value in ((row.get("research_features") or {}).get("available") or {}).items():
            presence[name].append(bool(value))
    coverage = sorted(
        (
            {
                "feature": name,
                "availability": round(sum(values) / len(values), 6),
                "rows": len(values),
            }
            for name, values in presence.items()
        ),
        key=lambda row: (row["availability"], row["feature"]),
    )
    return {
        "status": "eligible_for_shadow_ablation" if len(markets) >= 100 else "collecting",
        "rows": len(research),
        "independent_markets": len(markets),
        "minimum_independent_markets": 100,
        "affects_live_probability": False,
        "feature_availability_lowest": coverage[:20],
    }


def _research_values(row):
    return (row.get("research_features") or {}).get("values") or {}


def _select_research_features(rows, names, maximum=20):
    """Select on training data only using market-weighted residual correlation."""
    weights = market_sample_weights(rows)
    scored = []
    for name in names:
        present = [
            bool(((row.get("research_features") or {}).get("available") or {}).get(name))
            for row in rows
        ]
        coverage = sum(weight for weight, value in zip(weights, present) if value) / max(1e-12, sum(weights))
        if coverage < 0.75:
            continue
        values = [number(_research_values(row).get(name)) for row in rows]
        residuals = [
            int(row["label_yes"]) - number(row.get("market_probability"), 0.5)
            for row in rows
        ]
        total = max(1e-12, sum(weights))
        mean_x = sum(weight * value for weight, value in zip(weights, values)) / total
        mean_y = sum(weight * value for weight, value in zip(weights, residuals)) / total
        covariance = sum(
            weight * (value - mean_x) * (residual - mean_y)
            for weight, value, residual in zip(weights, values, residuals)
        ) / total
        variance = sum(weight * (value - mean_x) ** 2 for weight, value in zip(weights, values)) / total
        score = abs(covariance) / max(1e-9, math.sqrt(variance))
        if variance > 1e-10:
            scored.append((score, name))
    return [name for _score, name in sorted(scored, reverse=True)[:max(1, int(maximum))]]


def _train_research_residual(rows, feature_names, iterations=250, l2=0.08):
    weights_by_row = market_sample_weights(rows)
    vectors = [[number(_research_values(row).get(name)) for name in feature_names] for row in rows]
    total = max(1e-12, sum(weights_by_row))
    means = [
        sum(weight * values[index] for weight, values in zip(weights_by_row, vectors)) / total
        for index in range(len(feature_names))
    ]
    stds = []
    for index, mean in enumerate(means):
        variance = sum(
            weight * (values[index] - mean) ** 2
            for weight, values in zip(weights_by_row, vectors)
        ) / total
        stds.append(max(1e-6, math.sqrt(variance)))
    scaled = [scale_vector(values, means, stds) for values in vectors]
    coefficients = [0.0] * len(feature_names)
    intercept = 0.0
    for iteration in range(max(1, int(iterations))):
        gradient = [0.0] * len(coefficients)
        intercept_gradient = 0.0
        for values, row, sample_weight in zip(scaled, rows, weights_by_row):
            raw = logit(number(row.get("market_probability"), 0.5)) + intercept + sum(
                coefficient * value for coefficient, value in zip(coefficients, values)
            )
            error = sigmoid(raw) - int(row["label_yes"])
            intercept_gradient += sample_weight * error
            for index, value in enumerate(values):
                gradient[index] += sample_weight * error * value
        step = 0.06 / math.sqrt(1.0 + iteration / 40.0)
        intercept -= step * intercept_gradient / total
        for index in range(len(coefficients)):
            coefficients[index] -= step * (gradient[index] / total + l2 * coefficients[index])
    return {
        "features": list(feature_names),
        "means": means,
        "stds": stds,
        "weights": coefficients,
        "intercept": intercept,
    }


def _predict_research_residual(model, row):
    values = [number(_research_values(row).get(name)) for name in model.get("features") or []]
    scaled = scale_vector(values, model.get("means") or [], model.get("stds") or [])
    return sigmoid(
        logit(number(row.get("market_probability"), 0.5))
        + number(model.get("intercept"))
        + sum(number(weight) * value for weight, value in zip(model.get("weights") or [], scaled))
    )


def research_shadow_ablation(rows):
    """Compare research feature families without allowing them into live pricing."""
    research = [
        row for row in rows
        if row.get("research_feature_schema_version") == RESEARCH_FEATURE_SCHEMA_VERSION
        and isinstance(_research_values(row), dict)
    ]
    unique_markets = len({str(row.get("ticker") or "") for row in research})
    if unique_markets < 100:
        return {
            "status": "collecting",
            "independent_markets": unique_markets,
            "minimum_independent_markets": 100,
            "affects_live_probability": False,
            "families": [],
        }
    train_rows, validation_rows, test_rows = unique_market_split(research)
    all_names = sorted({name for row in train_rows for name in _research_values(row)})
    families = {
        "settlement": [name for name in all_names if any(token in name for token in ("target", "settlement", "basis", "minute_vol"))],
        "path_lead_lag": [name for name in all_names if "return_" in name or "velocity" in name or "acceleration" in name],
        "order_flow": [name for name in all_names if "trade_flow" in name or "trade_count" in name or "notional" in name],
        "book_execution": [name for name in all_names if any(token in name for token in ("imbalance", "depth", "spread", "microprice", "slope", "convexity", "latency"))],
        "market_context": [name for name in all_names if any(token in name for token in ("market_volume", "market_liquidity", "open_interest", "utc_time", "weekend", "btc_relative"))],
        "all_research": all_names,
    }
    validation_labels = [int(row["label_yes"]) for row in validation_rows]
    validation_keys = [str(row.get("ticker") or "") for row in validation_rows]
    test_labels = [int(row["label_yes"]) for row in test_rows]
    test_keys = [str(row.get("ticker") or "") for row in test_rows]
    validations = []
    models = {}
    for family, names in families.items():
        selected = _select_research_features(
            train_rows,
            names,
            maximum=20 if family == "all_research" else 12,
        )
        if not selected:
            continue
        model = _train_research_residual(train_rows, selected)
        models[family] = model
        probabilities = [_predict_research_residual(model, row) for row in validation_rows]
        metric = market_weighted_metrics(probabilities, validation_labels, validation_keys)
        market_metric = market_weighted_metrics(
            [number(row.get("market_probability"), 0.5) for row in validation_rows],
            validation_labels,
            validation_keys,
        )
        validations.append({
            "family": family,
            "features": selected,
            "validation": metric,
            "market_validation": market_metric,
            "validation_brier_improvement": round(number(market_metric.get("brier"), 1.0) - number(metric.get("brier"), 1.0), 6),
        })
    if not validations:
        return {
            "status": "insufficient_feature_coverage",
            "independent_markets": unique_markets,
            "affects_live_probability": False,
            "families": [],
        }
    selected = min(
        validations,
        key=lambda row: (
            number((row.get("validation") or {}).get("brier"), 1.0),
            number((row.get("validation") or {}).get("log_loss"), 99.0),
        ),
    )
    model = models[selected["family"]]
    test_probabilities = [_predict_research_residual(model, row) for row in test_rows]
    test_metric = market_weighted_metrics(test_probabilities, test_labels, test_keys)
    market_test = market_weighted_metrics(
        [number(row.get("market_probability"), 0.5) for row in test_rows],
        test_labels,
        test_keys,
    )
    brier_improvement = number(market_test.get("brier"), 1.0) - number(test_metric.get("brier"), 1.0)
    log_improvement = number(market_test.get("log_loss"), 99.0) - number(test_metric.get("log_loss"), 99.0)
    qualified = bool(
        selected["validation_brier_improvement"] > 0
        and brier_improvement > 0
        and log_improvement > 0
        and len(set(test_keys)) >= 20
    )
    return {
        "status": "shadow_qualified" if qualified else "shadow_not_qualified",
        "independent_markets": unique_markets,
        "test_markets": len(set(test_keys)),
        "affects_live_probability": False,
        "selected_family": selected["family"],
        "selected_features": selected["features"],
        "families": validations,
        "test": test_metric,
        "market_test": market_test,
        "test_brier_improvement": round(brier_improvement, 6),
        "test_log_loss_improvement": round(log_improvement, 6),
        "qualified_for_future_promotion_review": qualified,
        "promotion_requires_main_model_gates": True,
    }


def unique_market_split(rows):
    by_market = defaultdict(list)
    for row in rows:
        by_market[str(row.get("ticker") or "")].append(row)
    by_expiry = defaultdict(list)
    for ticker, values in by_market.items():
        expiry = str((values[0] or {}).get("close_time") or ticker)
        by_expiry[expiry].append(ticker)
    expiries = sorted(by_expiry)
    if len(expiries) < 10:
        return rows, [], []
    train_end = max(1, int(len(expiries) * 0.70))
    validation_end = max(train_end + 2, int(len(expiries) * 0.85))
    train_expiries = expiries[:max(1, train_end - 1)]
    validation_expiries = expiries[train_end:max(train_end + 1, validation_end - 1)]
    test_expiries = expiries[validation_end:]
    train_keys = {ticker for expiry in train_expiries for ticker in by_expiry[expiry]}
    validation_keys = {ticker for expiry in validation_expiries for ticker in by_expiry[expiry]}
    test_keys = {ticker for expiry in test_expiries for ticker in by_expiry[expiry]}
    return (
        [row for row in rows if row["ticker"] in train_keys],
        [row for row in rows if row["ticker"] in validation_keys],
        [row for row in rows if row["ticker"] in test_keys],
    )


def settlement_basis_by_asset(rows):
    grouped = defaultdict(list)
    for row in rows:
        value = row.get("settlement_basis_residual_bps")
        if value is None:
            continue
        parsed = number(value)
        if abs(parsed) <= 250.0:
            grouped[str(row.get("asset") or "").upper()].append(parsed)
    result = {}
    for asset, values in grouped.items():
        if not asset or not values:
            continue
        ordered = sorted(values)
        low = ordered[int((len(ordered) - 1) * 0.05)]
        high = ordered[int((len(ordered) - 1) * 0.95)]
        winsorized = [max(low, min(high, value)) for value in values]
        mean = sum(winsorized) / len(winsorized)
        variance = sum((value - mean) ** 2 for value in winsorized) / max(1, len(winsorized) - 1)
        result[asset] = {
            "samples": len(values),
            "mean_bps": round(mean, 6),
            "median_bps": round(ordered[len(ordered) // 2], 6),
            "std_bps": round(math.sqrt(variance), 6),
            "method": "winsorized_5_95_percent",
        }
    return result


def train_walk_forward_state(
    rows,
    minimum_shadow_markets=40,
    minimum_active_markets=300,
    minimum_brier_improvement=0.005,
    midrange_max_calibration_gap=0.05,
    minimum_interval_markets=100,
    minimum_interval_coverage=0.80,
):
    unique_markets = len({row.get("ticker") for row in rows})
    research_readiness = research_feature_readiness(rows)
    research_ablation = research_shadow_ablation(rows)
    state = {
        "version": 3,
        "model_architecture": "market_residual_ensemble_v2",
        "evaluation_revision": EVALUATION_REVISION,
        "sample_weighting": "one_total_weight_per_market",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "feature_names": list(FEATURE_NAMES),
        "rows": len(rows),
        "unique_markets": unique_markets,
        "status": "collecting",
        "active": False,
        "activation_reason": "insufficient_resolved_markets",
        "settlement_basis_by_asset": settlement_basis_by_asset(rows),
        "research_feature_readiness": research_readiness,
        "research_shadow_ablation": research_ablation,
    }
    if unique_markets < int(minimum_shadow_markets):
        return state
    train_rows, validation_rows, test_rows = unique_market_split(rows)
    if not validation_rows or not test_rows:
        state["activation_reason"] = "insufficient_walk_forward_split"
        return state
    logistic_model = train_logistic(train_rows)
    boosted_model = train_boosted_stumps(train_rows)
    validation_labels = [int(row["label_yes"]) for row in validation_rows]
    validation_logistic = [
        predict_logistic(logistic_model, row)
        for row in validation_rows
    ]
    validation_boosted = [
        predict_boosted(boosted_model, row)
        for row in validation_rows
    ]
    blend_evaluations = []
    validation_market_keys = [row["ticker"] for row in validation_rows]
    for logistic_weight in ENSEMBLE_LOGISTIC_WEIGHTS:
        weights = {
            "logistic": logistic_weight,
            "boosted": 1.0 - logistic_weight,
        }
        probabilities = [
            blend_probability(logistic, boosted, weights)
            for logistic, boosted in zip(validation_logistic, validation_boosted)
        ]
        blend_evaluations.append({
            "logistic_weight": logistic_weight,
            "boosted_weight": 1.0 - logistic_weight,
            **market_weighted_metrics(
                probabilities,
                validation_labels,
                validation_market_keys,
            ),
            "snapshot_metrics": metrics(probabilities, validation_labels),
        })
    selected_blend = min(
        blend_evaluations,
        key=lambda row: (
            number(row.get("brier"), 1.0),
            number(row.get("log_loss"), 99.0),
            -number(row.get("logistic_weight")),
        ),
    )
    ensemble_weights = {
        "logistic": number(selected_blend.get("logistic_weight"), 0.5),
        "boosted": number(selected_blend.get("boosted_weight"), 0.5),
        "selection": "validation_brier_then_log_loss",
    }
    validation_raw = [
        blend_probability(logistic, boosted, ensemble_weights)
        for logistic, boosted in zip(validation_logistic, validation_boosted)
    ]
    validation_weights = market_sample_weights(validation_rows)
    global_calibrator = regularized_calibrator(fit_platt(
        validation_raw,
        validation_labels,
        sample_weights=validation_weights,
    ))
    subgroup_calibrators = {}
    grouped = defaultdict(list)
    for probability, row in zip(validation_raw, validation_rows):
        grouped[f"{row.get('asset')}|{row.get('minute_bucket')}"].append(
            (probability, int(row["label_yes"]), row)
        )
    for key, values in grouped.items():
        subgroup_rows = [row for _probability, _label, row in values]
        subgroup_markets = len({str(row.get("ticker") or "") for row in subgroup_rows})
        if subgroup_markets >= 30 and len({label for _probability, label, _row in values}) >= 2:
            subgroup_calibrators[key] = regularized_calibrator(
                fit_platt(
                    [probability for probability, _label, _row in values],
                    [label for _probability, label, _row in values],
                    sample_weights=market_sample_weights(subgroup_rows),
                )
            )

    def base_calibrated_probability(row):
        raw = blend_probability(
            predict_logistic(logistic_model, row),
            predict_boosted(boosted_model, row),
            ensemble_weights,
        )
        calibrator = subgroup_calibrators.get(
            f"{row.get('asset')}|{row.get('minute_bucket')}",
            global_calibrator,
        )
        return apply_platt(raw, calibrator)

    validation_base_probabilities = [
        base_calibrated_probability(row)
        for row in validation_rows
    ]
    confidence_band_calibrators = {}
    grouped_confidence = defaultdict(list)
    for probability, row in zip(validation_base_probabilities, validation_rows):
        grouped_confidence[confidence_bucket(probability)].append(
            (probability, int(row["label_yes"]), row)
        )
    for key, values in grouped_confidence.items():
        confidence_rows = [row for _probability, _label, row in values]
        confidence_markets = len({str(row.get("ticker") or "") for row in confidence_rows})
        if confidence_markets >= 40 and len({label for _probability, label, _row in values}) >= 2:
            confidence_band_calibrators[key] = regularized_calibrator(
                fit_platt(
                    [probability for probability, _label, _row in values],
                    [label for _probability, label, _row in values],
                    sample_weights=market_sample_weights(confidence_rows),
                )
            )

    def probability_before_empirical_interval(row):
        probability = base_calibrated_probability(row)
        band_calibrator = confidence_band_calibrators.get(
            confidence_bucket(probability)
        )
        if band_calibrator:
            probability = apply_platt(probability, band_calibrator)
        return probability

    validation_final_probabilities = [
        probability_before_empirical_interval(row)
        for row in validation_rows
    ]
    empirical_calibration = fit_empirical_probability_calibration(
        validation_final_probabilities,
        validation_labels,
        validation_rows,
    )

    def ensemble_probability(row):
        probability = probability_before_empirical_interval(row)
        interval = empirical_probability_interval(
            empirical_calibration,
            row,
            probability,
        )
        return interval["probability"] if interval.get("available") else probability

    test_labels = [int(row["label_yes"]) for row in test_rows]
    logistic_probabilities = [predict_logistic(logistic_model, row) for row in test_rows]
    boosted_probabilities = [predict_boosted(boosted_model, row) for row in test_rows]
    ensemble_probabilities = [ensemble_probability(row) for row in test_rows]
    market_probabilities = [number(row.get("market_probability"), 0.5) for row in test_rows]
    heuristic_probabilities = [number(row.get("heuristic_probability"), 0.5) for row in test_rows]
    evaluation = {
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "test_rows": len(test_rows),
        "train_markets": len({row["ticker"] for row in train_rows}),
        "validation_markets": len({row["ticker"] for row in validation_rows}),
        "test_markets": len({row["ticker"] for row in test_rows}),
        "market_baseline": metrics(market_probabilities, test_labels),
        "heuristic_baseline": metrics(heuristic_probabilities, test_labels),
        "logistic": metrics(logistic_probabilities, test_labels),
        "boosted": metrics(boosted_probabilities, test_labels),
        "calibrated_ensemble": metrics(ensemble_probabilities, test_labels),
        "market_weighted_market_baseline": market_weighted_metrics(
            market_probabilities,
            test_labels,
            [row["ticker"] for row in test_rows],
        ),
        "market_weighted_calibrated_ensemble": market_weighted_metrics(
            ensemble_probabilities,
            test_labels,
            [row["ticker"] for row in test_rows],
        ),
        "after_fee_policy": after_fee_policy_metrics(
            ensemble_probabilities,
            test_rows,
        ),
    }
    evaluation["selected_side_midrange"] = selected_side_calibration(
        ensemble_probabilities,
        test_labels,
        low=0.55,
        high=0.65,
        market_keys=[row["ticker"] for row in test_rows],
    )
    evaluation["probability_interval"] = evaluate_empirical_probability_intervals(
        ensemble_probabilities,
        test_labels,
        test_rows,
        empirical_calibration,
    )
    evaluation["probability_tier_policy"] = probability_tier_policy_metrics(
        ensemble_probabilities,
        test_rows,
    )
    evaluation["conditional_reliability"] = conditional_reliability(
        ensemble_probabilities,
        test_rows,
    )
    evaluation["temporal_stability"] = temporal_stability_windows(
        ensemble_probabilities,
        test_rows,
    )
    evaluation["feature_drift"] = feature_drift_summary(
        train_rows,
        test_rows,
    )
    evaluation["research_feature_readiness"] = research_readiness
    evaluation["research_shadow_ablation"] = research_ablation
    ensemble_brier = number(evaluation["calibrated_ensemble"].get("brier"), 1.0)
    market_brier = number(evaluation["market_baseline"].get("brier"), 1.0)
    ensemble_log_loss = number(evaluation["calibrated_ensemble"].get("log_loss"), 99.0)
    market_log_loss = number(evaluation["market_baseline"].get("log_loss"), 99.0)
    market_weighted_ensemble_brier = number(
        evaluation["market_weighted_calibrated_ensemble"].get("brier"),
        1.0,
    )
    market_weighted_baseline_brier = number(
        evaluation["market_weighted_market_baseline"].get("brier"),
        1.0,
    )
    policy_roi = number(evaluation["after_fee_policy"].get("roi"), -1.0)
    policy_markets = int(number(evaluation["after_fee_policy"].get("markets")))
    midrange_calibration = evaluation["selected_side_midrange"]
    midrange_samples = int(number(midrange_calibration.get("samples")))
    midrange_markets = int(number(midrange_calibration.get("markets")))
    midrange_gap = number(midrange_calibration.get("calibration_gap"), 1.0)
    interval_evaluation = evaluation["probability_interval"]
    interval_markets = int(number(interval_evaluation.get("markets")))
    interval_coverage = number(interval_evaluation.get("coverage"), 0.0)
    temporal_stability_qualified = bool(
        (evaluation.get("temporal_stability") or {}).get("qualified")
    )
    feature_drift_qualified = bool(
        (evaluation.get("feature_drift") or {}).get("qualified")
    )
    active = (
        unique_markets >= int(minimum_active_markets)
        and evaluation["test_markets"] >= 40
        and ensemble_brier + float(minimum_brier_improvement) <= market_brier
        and ensemble_log_loss < market_log_loss
        and market_weighted_ensemble_brier + float(minimum_brier_improvement) <= market_weighted_baseline_brier
        and policy_markets >= 40
        and policy_roi > 0
        and midrange_markets >= 100
        and midrange_gap <= float(midrange_max_calibration_gap)
        and interval_markets >= int(minimum_interval_markets)
        and interval_coverage >= float(minimum_interval_coverage)
        and temporal_stability_qualified
        and feature_drift_qualified
    )
    state.update({
        "status": "active" if active else "shadow",
        "active": active,
        "activation_reason": "walk_forward_outperforms_market" if active else "walk_forward_not_yet_qualified",
        "logistic_model": logistic_model,
        "boosted_model": boosted_model,
        "ensemble_weights": ensemble_weights,
        "ensemble_selection": {
            "candidates": blend_evaluations,
            "selected": selected_blend,
            "calibration_mode": "regularized_platt",
        },
        "global_calibrator": global_calibrator,
        "subgroup_calibrators": subgroup_calibrators,
        "confidence_band_calibrators": confidence_band_calibrators,
        "empirical_probability_calibration": empirical_calibration,
        "activation_checks": {
            "minimum_active_markets": int(minimum_active_markets),
            "minimum_brier_improvement": float(minimum_brier_improvement),
            "midrange_minimum_markets": 100,
            "midrange_max_calibration_gap": float(midrange_max_calibration_gap),
            "midrange_samples": midrange_samples,
            "midrange_markets": midrange_markets,
            "midrange_calibration_gap": round(midrange_gap, 6),
            "market_weighted_brier_improvement": round(
                market_weighted_baseline_brier - market_weighted_ensemble_brier,
                6,
            ),
            "after_fee_policy_markets": policy_markets,
            "after_fee_policy_roi": round(policy_roi, 6),
            "minimum_interval_markets": int(minimum_interval_markets),
            "minimum_interval_coverage": float(minimum_interval_coverage),
            "interval_markets": interval_markets,
            "interval_coverage": round(interval_coverage, 6),
            "temporal_stability_qualified": temporal_stability_qualified,
            "feature_drift_qualified": feature_drift_qualified,
        },
        "evaluation": evaluation,
    })
    return state


def load_or_train_state(
    dataset_file,
    state_file,
    retrain_minutes=60.0,
    minimum_shadow_markets=40,
    minimum_active_markets=300,
    minimum_brier_improvement=0.005,
    midrange_max_calibration_gap=0.05,
    minimum_interval_markets=100,
    minimum_interval_coverage=0.80,
    force=False,
):
    dataset_file = Path(dataset_file)
    state_file = Path(state_file)
    state = read_json(state_file, {})
    if state and state.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        force = True
    if state and state.get("model_architecture") != "market_residual_ensemble_v2":
        force = True
    if state and state.get("evaluation_revision") != EVALUATION_REVISION:
        force = True
    generated_at = parse_time(state.get("generated_at"))
    fresh = (
        generated_at
        and datetime.now(timezone.utc) - generated_at < timedelta(minutes=float(retrain_minutes))
    )
    dataset_newer_than_collecting_state = False
    if (
        state.get("status") == "collecting"
        and generated_at
        and dataset_file.exists()
    ):
        dataset_updated_at = datetime.fromtimestamp(
            dataset_file.stat().st_mtime,
            tz=timezone.utc,
        )
        dataset_newer_than_collecting_state = dataset_updated_at > generated_at
    if state and fresh and not force and not dataset_newer_than_collecting_state:
        return state
    rows = load_dataset(dataset_file)
    state = train_walk_forward_state(
        rows,
        minimum_shadow_markets=minimum_shadow_markets,
        minimum_active_markets=minimum_active_markets,
        minimum_brier_improvement=minimum_brier_improvement,
        midrange_max_calibration_gap=midrange_max_calibration_gap,
        minimum_interval_markets=minimum_interval_markets,
        minimum_interval_coverage=minimum_interval_coverage,
    )
    atomic_write_json(state_file, state)
    return state


def predict_candidate(candidate, state):
    if not state or state.get("status") == "collecting":
        return {
            "available": False,
            "active": False,
            "status": (state or {}).get("status", "collecting"),
            "reason": (state or {}).get("activation_reason", "model_unavailable"),
        }
    anchor = (candidate.get("probability") or {}).get("market_implied_yes")
    if anchor is None or not math.isfinite(number(anchor)) or not 0 < number(anchor) < 100:
        return {"available": False, "active": False, "status": "unavailable", "reason": "market_probability_anchor_missing"}
    row = {
        "asset": candidate.get("asset"),
        "minute_bucket": minute_bucket(candidate.get("minutes_to_close")),
        "features": candidate_feature_payload(candidate),
        "market_probability": number(anchor) / 100.0,
    }
    logistic_probability = predict_logistic(state["logistic_model"], row)
    boosted_probability = predict_boosted(state["boosted_model"], row)
    raw_ensemble = blend_probability(
        logistic_probability,
        boosted_probability,
        state.get("ensemble_weights"),
    )
    calibrator_key = f"{row.get('asset')}|{row.get('minute_bucket')}"
    calibrator = (state.get("subgroup_calibrators") or {}).get(
        calibrator_key,
        state.get("global_calibrator") or {"slope": 1.0, "intercept": 0.0},
    )
    probability = apply_platt(raw_ensemble, calibrator)
    confidence_key = confidence_bucket(probability)
    confidence_calibrator = (state.get("confidence_band_calibrators") or {}).get(
        confidence_key
    )
    if confidence_calibrator:
        probability = apply_platt(probability, confidence_calibrator)
    empirical_interval = empirical_probability_interval(
        state.get("empirical_probability_calibration") or {},
        row,
        probability,
    )
    if empirical_interval.get("available"):
        probability = empirical_interval["probability"]
    evaluation = state.get("evaluation") or {}
    calibration_gap = max(
        number((evaluation.get("selected_side_midrange") or {}).get("calibration_gap")),
        number((evaluation.get("probability_interval") or {}).get("maximum_calibration_gap")),
    )
    return {
        "available": True,
        "active": bool(state.get("active")),
        "status": state.get("status"),
        "reason": state.get("activation_reason"),
        "prob_yes": round(probability * 100.0, 2),
        "prob_yes_low": round(empirical_interval.get("low", probability) * 100.0, 2),
        "prob_yes_high": round(empirical_interval.get("high", probability) * 100.0, 2),
        "logistic_prob_yes": round(logistic_probability * 100.0, 2),
        "boosted_prob_yes": round(boosted_probability * 100.0, 2),
        "ensemble_weights": state.get("ensemble_weights") or {
            "logistic": 0.5,
            "boosted": 0.5,
        },
        "calibration_scope": (
            f"{calibrator_key if calibrator_key in (state.get('subgroup_calibrators') or {}) else 'global'}"
            f"|confidence:{confidence_key if confidence_calibrator else 'global'}"
        ),
        "empirical_interval": empirical_interval,
        "interval_qualified": bool(
            (evaluation.get("probability_interval") or {}).get("markets", 0) >= 100
            and number((evaluation.get("probability_interval") or {}).get("coverage")) >= 0.80
        ),
        "calibration_error_reserve_pp": round(
            max(0.5, min(5.0, calibration_gap * 50.0)),
            4,
        ),
        "unique_markets": state.get("unique_markets", 0),
        "qualified_for_activation": bool(
            state.get("active") or state.get("qualified_for_activation")
        ),
        "model_architecture": state.get("model_architecture"),
        "sample_weighting": state.get("sample_weighting"),
        "evaluation": evaluation,
    }
