"""Shared classification, Pyth feeds, and risk helpers for short-horizon markets.

This module deliberately contains no order-placement code.  It gives the live
bot settlement-aligned commodity inputs and stable portfolio grouping without
allowing a missing or stale feed to silently fall back to an unrelated proxy.
"""

from __future__ import annotations

import math
import re
import threading
import time
from datetime import datetime, timezone

import requests


COMMODITY_ASSETS = {"GOLD", "SILVER", "WTI"}
CRYPTO_ASSETS = {"BTC", "ETH", "SOL", "XRP", "DOGE"}

COMMODITY_SERIES = {
    "KXGOLD15M": "GOLD",
    "KXGOLDH": "GOLD",
    "KXSILVER15M": "SILVER",
    "KXSILVERH": "SILVER",
    "KXWTI15M": "WTI",
    "KXWTIH": "WTI",
}

CRYPTO_HOURLY_SERIES = {
    "KXBTCD": "BTC",
    "KXBTC": "BTC",
    "KXETHD": "ETH",
    "KXETH": "ETH",
    "KXSOLD": "SOL",
    "KXSOL": "SOL",
    "KXXRPD": "XRP",
    "KXXRP": "XRP",
    "KXDOGED": "DOGE",
    "KXDOGE": "DOGE",
}

PYTH_SYMBOL_QUERIES = {
    # These are the exact continuous index feeds named in Kalshi's settlement
    # sources.  They must not be replaced by spot metals or rolling futures.
    "GOLD": ("GOLD", "Metal.Index.GOLD/USD"),
    "SILVER": ("SILVER", "Metal.Index.SILVER/USD"),
    "WTI": ("PYTHOIL", "Commodities.Index.PYTHOIL/USD"),
}

# Public Pyth Core feeds are not the exact indices used for settlement.  They
# are used only to transport the most recent exact Kalshi opening value by the
# proxy's percentage return.  This keeps futures/spot basis out of the model.
PYTH_PROXY_SYMBOLS = {
    "GOLD": "Metal.XAU/USD",
    "SILVER": "Metal.XAG/USD",
}

_FUTURES_MONTHS = {code: month for month, code in enumerate("FGHJKMNQUVXZ", 1)}

_SYMBOL_CACHE = {}
_SYMBOL_CACHE_AT = 0.0
_ANCHOR_PRICE_CACHE = {}
_PYTH_HISTORY_CACHE = {}
_TWELVE_CONFIRMATION_CACHE = {}
_CACHE_LOCK = threading.Lock()
PYTH_HISTORY_CACHE_SECONDS = 45.0
TWELVE_CONFIRMATION_CACHE_SECONDS = 30.0


def price_momentum_score(closes):
    """Collapse one provider's price history into one bounded direction vote.

    Multiple horizons improve stability, but they remain a single source vote.
    This prevents one Pyth series from masquerading as several confirmations.
    """
    values = [float(value) for value in (closes or []) if float(value) > 0]
    if len(values) < 6:
        return 0.0
    returns = [
        math.log(values[index] / values[index - 1])
        for index in range(1, len(values))
        if values[index - 1] > 0 and values[index] > 0
    ]
    recent = returns[-60:]
    if not recent:
        return 0.0
    mean = sum(recent) / len(recent)
    variance = sum((value - mean) ** 2 for value in recent) / max(1, len(recent) - 1)
    volatility = max(math.sqrt(variance), 1e-7)

    def normalized(window):
        available = min(int(window), len(values) - 1)
        movement = math.log(values[-1] / values[-1 - available])
        return movement / max(volatility * math.sqrt(available), 1e-7)

    score = (
        0.20 * math.tanh(normalized(1))
        + 0.50 * math.tanh(normalized(5))
        + 0.30 * math.tanh(normalized(15))
    )
    return max(-1.0, min(1.0, score))


def _series(market):
    explicit = str((market or {}).get("series_ticker") or "").upper()
    if explicit:
        return explicit
    event = str((market or {}).get("event_ticker") or "").upper()
    ticker = str((market or {}).get("ticker") or "").upper()
    return event.split("-", 1)[0] or ticker.split("-", 1)[0]


def market_lane(market):
    series = _series(market)
    if series in COMMODITY_SERIES:
        return "commodity_15m" if series.endswith("15M") else "commodity_hourly"
    if series in CRYPTO_HOURLY_SERIES:
        return "crypto_hourly"
    if series.endswith("15M"):
        return "crypto_15m"
    return "unsupported"


def infer_market_asset(market):
    series = _series(market)
    if series in COMMODITY_SERIES:
        return COMMODITY_SERIES[series]
    if series in CRYPTO_HOURLY_SERIES:
        return CRYPTO_HOURLY_SERIES[series]
    return ""


def exposure_group(asset):
    asset = str(asset or "").upper()
    if asset in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
        return "crypto"
    if asset in {"GOLD", "SILVER"}:
        return "metals"
    if asset == "WTI":
        return "energy"
    return asset.lower() or "unknown"


def position_cluster_key(row):
    """Prevent overlapping horizons/strikes from masquerading as diversification."""
    asset = str((row or {}).get("asset") or "").upper()
    close = str((row or {}).get("close_time") or "")
    return f"{asset}|{close}"


def _headers(api_key):
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _stable_symbol_rows(query, timeout=10):
    response = requests.get(
        "https://pyth.dourolabs.app/v1/symbols",
        params={"query": query},
        timeout=timeout,
    )
    response.raise_for_status()
    return [row for row in response.json() if row.get("state") == "stable"]


def resolve_pyth_feed(asset, timeout=10):
    global _SYMBOL_CACHE_AT
    asset = str(asset or "").upper()
    now = time.time()
    if now - _SYMBOL_CACHE_AT > 3600:
        _SYMBOL_CACHE.clear()
    if asset in _SYMBOL_CACHE:
        return dict(_SYMBOL_CACHE[asset])
    query, exact_symbol = PYTH_SYMBOL_QUERIES[asset]
    rows = _stable_symbol_rows(query, timeout=timeout)
    row = next((item for item in rows if item.get("symbol") == exact_symbol), None)
    if not row or not row.get("hermes_id"):
        raise ValueError(f"pyth_feed_unavailable_{asset.lower()}")
    selected = {
        "asset": asset,
        "symbol": row.get("symbol"),
        "lazer_id": row.get("pyth_lazer_id"),
        "hermes_id": row.get("hermes_id"),
        "schedule": row.get("schedule"),
        "description": row.get("description"),
    }
    _SYMBOL_CACHE[asset] = selected
    _SYMBOL_CACHE_AT = now
    return dict(selected)


def _wti_contract_order(symbol, now=None):
    now = now or datetime.now(timezone.utc)
    match = re.search(r"WTI([FGHJKMNQUVXZ])(\d)/USD$", str(symbol or ""))
    if not match:
        return 999999
    month = _FUTURES_MONTHS.get(match.group(1), 12)
    year_digit = int(match.group(2))
    decade = (now.year // 10) * 10
    year = decade + year_digit
    if year < now.year - 1:
        year += 10
    value = year * 12 + month
    current = now.year * 12 + now.month
    return value if value >= current else value + 120


def resolve_pyth_proxy_feed(asset, timeout=10):
    """Resolve a liquid Core proxy, never mislabeled as the settlement feed."""
    global _SYMBOL_CACHE_AT
    asset = str(asset or "").upper()
    cache_key = f"proxy:{asset}"
    now = time.time()
    if now - _SYMBOL_CACHE_AT > 3600:
        _SYMBOL_CACHE.clear()
    if cache_key in _SYMBOL_CACHE:
        return dict(_SYMBOL_CACHE[cache_key])
    query = "WTI" if asset == "WTI" else ("XAU" if asset == "GOLD" else "XAG")
    rows = _stable_symbol_rows(query, timeout=timeout)
    if asset == "WTI":
        rows = [row for row in rows if re.search(r"Commodities\.WTI[FGHJKMNQUVXZ]\d/USD$", str(row.get("symbol") or ""))]
        rows.sort(key=lambda row: _wti_contract_order(row.get("symbol")))
        row = rows[0] if rows else None
    else:
        exact = PYTH_PROXY_SYMBOLS[asset]
        row = next((item for item in rows if item.get("symbol") == exact), None)
    if not row or not row.get("hermes_id") or row.get("pyth_lazer_id") in (None, ""):
        raise ValueError(f"pyth_proxy_feed_unavailable_{asset.lower()}")
    selected = {
        "asset": asset,
        "symbol": row.get("symbol"),
        "lazer_id": row.get("pyth_lazer_id"),
        "hermes_id": row.get("hermes_id"),
        "schedule": row.get("schedule"),
        "description": row.get("description"),
        "is_settlement_feed": False,
    }
    _SYMBOL_CACHE[cache_key] = selected
    _SYMBOL_CACHE_AT = now
    return dict(selected)


def fetch_pyth_latest(feed, api_key="", timeout=10, channel="fixed_rate@1000ms"):
    if not api_key:
        raise ValueError("pyth_api_key_required_for_latest")
    response = requests.post(
        "https://pyth-lazer.dourolabs.app/v1/latest_price",
        json={
            "priceFeedIds": [int(feed["lazer_id"])],
            "properties": [
                "price",
                "confidence",
                "exponent",
                "feedUpdateTimestamp",
                "marketSession",
            ],
            "formats": ["leUnsigned"],
            "channel": str(channel),
        },
        headers=_headers(api_key),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    parsed = payload.get("parsed") or payload
    feeds = parsed.get("priceFeeds") or parsed.get("price_feeds") or []
    if not feeds:
        raise ValueError("pyth_latest_price_missing")
    price = feeds[0]
    exponent = int(price.get("exponent") or 0)
    value = float(price.get("price")) * (10.0 ** exponent)
    confidence = float(price.get("confidence") or 0) * (10.0 ** exponent)
    update_us = int(price.get("feedUpdateTimestamp") or price.get("feed_update_timestamp") or 0)
    publish_time = update_us / 1_000_000.0 if update_us else 0.0
    age = max(0.0, time.time() - publish_time) if publish_time else math.inf
    return {
        "spot": value,
        "pyth_confidence_interval": confidence,
        "publish_time": datetime.fromtimestamp(publish_time, timezone.utc).isoformat() if publish_time else None,
        "age_seconds": round(age, 3),
        "market_session": price.get("marketSession") or price.get("market_session"),
    }


def fetch_pyth_history(feed, api_key, lookback_minutes=720, timeout=15, channel="fixed_rate@1000ms", include_times=False):
    if not api_key:
        raise ValueError("pyth_api_key_required_for_history")
    cache_key = (str(feed.get("symbol") or ""), int(lookback_minutes), str(channel))
    now_monotonic = time.monotonic()
    with _CACHE_LOCK:
        cached = _PYTH_HISTORY_CACHE.get(cache_key)
        if cached and now_monotonic - cached["cached_at"] <= PYTH_HISTORY_CACHE_SECONDS:
            observations = list(cached["observations"])
            if include_times:
                return {
                    "times": [ts for ts, _ in observations],
                    "closes": [value for _, value in observations],
                    "cache_hit": True,
                }
            return [value for _, value in observations]
    end = int(time.time())
    start = end - max(120, int(lookback_minutes)) * 60
    response = requests.get(
        f"https://pyth.dourolabs.app/v1/{channel}/history",
        params={
            "symbol": feed["symbol"],
            "from": start,
            "to": end,
            "resolution": "1",
        },
        headers=_headers(api_key),
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("s") != "ok":
        raise ValueError(f"pyth_history_error_{payload.get('errmsg') or payload.get('s')}")
    raw_times = payload.get("t") or []
    raw_closes = payload.get("c") or []
    observations = [
        (int(ts), float(value))
        for ts, value in zip(raw_times, raw_closes)
        if float(value) > 0
    ]
    closes = [value for _, value in observations]
    if len(closes) < 60:
        raise ValueError(f"pyth_history_short_{len(closes)}")
    with _CACHE_LOCK:
        _PYTH_HISTORY_CACHE[cache_key] = {
            "cached_at": now_monotonic,
            "observations": tuple(observations),
        }
    if include_times:
        return {
            "times": [ts for ts, _ in observations],
            "closes": closes,
            "cache_hit": False,
        }
    return closes


def fetch_pyth_price_at(feed, timestamp, api_key="", timeout=10):
    """Get the first Core proxy update at an immutable anchor timestamp."""
    timestamp = int(timestamp)
    cache_key = (str(feed.get("hermes_id") or ""), timestamp)
    if cache_key in _ANCHOR_PRICE_CACHE:
        return dict(_ANCHOR_PRICE_CACHE[cache_key])
    response = requests.get(
        f"https://hermes.pyth.network/v2/updates/price/{timestamp}",
        params={"ids[]": feed["hermes_id"], "parsed": "true"},
        headers=_headers(api_key),
        timeout=timeout,
    )
    response.raise_for_status()
    parsed = response.json().get("parsed") or []
    if not parsed:
        raise ValueError("pyth_proxy_anchor_missing")
    price_row = parsed[0].get("price") or {}
    value = float(price_row.get("price")) * (10.0 ** int(price_row.get("expo") or 0))
    publish_time = int(price_row.get("publish_time") or timestamp)
    result = {"price": value, "publish_time": publish_time}
    _ANCHOR_PRICE_CACHE[cache_key] = result
    return dict(result)


def fetch_twelve_gold_confirmation(api_key, timeout=10):
    if not api_key:
        return None
    now_monotonic = time.monotonic()
    with _CACHE_LOCK:
        cached = _TWELVE_CONFIRMATION_CACHE.get("GOLD")
        if cached and now_monotonic - cached["cached_at"] <= TWELVE_CONFIRMATION_CACHE_SECONDS:
            result = dict(cached["value"])
            published = datetime.fromisoformat(str(result["publish_time"]).replace("Z", "+00:00"))
            result["age_seconds"] = round(
                max(0.0, (datetime.now(timezone.utc) - published).total_seconds()),
                3,
            )
            return result
    response = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": "XAU/USD",
            "interval": "1min",
            "outputsize": 16,
            "timezone": "UTC",
            "apikey": api_key,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    values = payload.get("values") or []
    if not values:
        raise ValueError(f"twelve_data_gold_unavailable_{payload.get('code') or 'empty'}")
    row = values[0]
    timestamp = datetime.fromisoformat(str(row["datetime"]).replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    closes = [float(value["close"]) for value in reversed(values) if float(value.get("close") or 0) > 0]
    result = {
        "price": float(row["close"]),
        "publish_time": timestamp.isoformat(),
        "age_seconds": round(age_seconds, 3),
        "history_points": len(closes),
        "momentum_score": round(price_momentum_score(closes), 6),
        "source": "twelve_data_xauusd",
    }
    with _CACHE_LOCK:
        _TWELVE_CONFIRMATION_CACHE["GOLD"] = {
            "cached_at": now_monotonic,
            "value": dict(result),
        }
    return result


def fetch_commodity_proxy_snapshot(asset, api_key="", twelve_api_key="", lookback_minutes=720, max_age_seconds=15, timeout=15, channel="fixed_rate@1000ms"):
    """Fetch liquid Core movement data that still requires a Kalshi anchor."""
    asset = str(asset).upper()
    feed = resolve_pyth_proxy_feed(asset, timeout=timeout)
    latest = fetch_pyth_latest(feed, api_key=api_key, timeout=timeout, channel=channel)
    if latest["age_seconds"] > float(max_age_seconds):
        raise ValueError(f"pyth_proxy_stale_{latest['age_seconds']:.1f}s")
    history = fetch_pyth_history(
        feed, api_key=api_key, lookback_minutes=lookback_minutes,
        timeout=timeout, channel=channel, include_times=True,
    )
    closes = history["closes"]
    if closes and closes[-1] > 0:
        factor = latest["spot"] / closes[-1]
        closes = [value * factor for value in closes]
    pyth_momentum = price_momentum_score(closes)
    confirmation = None
    if asset == "GOLD" and twelve_api_key:
        try:
            confirmation = fetch_twelve_gold_confirmation(twelve_api_key, timeout=min(timeout, 10))
            confirmation["deviation_bps"] = round(
                abs(confirmation["price"] / latest["spot"] - 1.0) * 10000.0, 3
            )
            confirmation["valid"] = bool(
                confirmation.get("age_seconds", math.inf) <= 180.0
                and confirmation["deviation_bps"] <= 50.0
                and confirmation.get("history_points", 0) >= 6
            )
        except Exception as exc:
            confirmation = {"error": type(exc).__name__}
    source_scores = {"pyth_price_momentum": pyth_momentum}
    if confirmation and confirmation.get("valid"):
        source_scores["twelve_data_xauusd"] = float(confirmation.get("momentum_score") or 0.0)
    source_values = list(source_scores.values())
    underlying_score = sum(source_values) / len(source_values) if source_values else 0.0
    return {
        "asset": asset,
        "source": "pyth_core_proxy",
        "settlement_source": "Pyth",
        "spot": latest["spot"],
        "closes": closes,
        "history_times": history["times"],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "requires_kalshi_anchor": True,
        "pyth_proxy": {**feed, **latest, "history_points": len(closes)},
        "twelve_data_confirmation": confirmation,
        "microstructure": {
            "settlement_proxy_spot": latest["spot"],
            "settlement_proxy_source_count": 1 + int(bool(confirmation and not confirmation.get("error"))),
            "cross_exchange_dispersion_bps": float((confirmation or {}).get("deviation_bps") or 0),
            "underlying_flow_score": underlying_score,
            "underlying_flow_strength": abs(underlying_score),
            "underlying_flow_agreement": bool(
                len(source_values) >= 2
                and (all(value >= 0 for value in source_values) or all(value <= 0 for value in source_values))
            ),
            "source_flow_scores": source_scores,
            "source": "kalshi_anchor_required_pyth_core_proxy",
        },
    }


def fetch_commodity_snapshot(asset, api_key="", lookback_minutes=720, max_age_seconds=15, timeout=15, channel="fixed_rate@1000ms"):
    feed = resolve_pyth_feed(asset, timeout=timeout)
    latest = fetch_pyth_latest(feed, api_key=api_key, timeout=timeout, channel=channel)
    if latest["age_seconds"] > float(max_age_seconds):
        raise ValueError(f"pyth_price_stale_{latest['age_seconds']:.1f}s")
    closes = fetch_pyth_history(
        feed,
        api_key=api_key,
        lookback_minutes=lookback_minutes,
        timeout=timeout,
        channel=channel,
    )
    # Align the completed one-minute history to the latest settlement proxy.
    # This preserves returns while removing a harmless partial-candle offset.
    if closes and closes[-1] > 0:
        factor = latest["spot"] / closes[-1]
        closes = [value * factor for value in closes]
    pyth_momentum = price_momentum_score(closes)
    return {
        "asset": str(asset).upper(),
        "source": "pyth",
        "settlement_source": "Pyth",
        "spot": latest["spot"],
        "closes": closes,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "pyth": {**feed, **latest, "history_points": len(closes)},
        "microstructure": {
            "settlement_proxy_spot": latest["spot"],
            "settlement_proxy_source_count": 1,
            "cross_exchange_dispersion_bps": 0.0,
            "underlying_flow_score": pyth_momentum,
            "underlying_flow_strength": abs(pyth_momentum),
            "underlying_flow_agreement": False,
            "source_flow_scores": {"pyth_price_momentum": pyth_momentum},
            "source": "pyth_settlement_proxy",
        },
    }
