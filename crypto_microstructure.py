"""Short-horizon crypto market-data features with graceful REST fallbacks."""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import zlib
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests

try:
    import websockets
except Exception:  # Optional at import time; REST remains available.
    websockets = None


COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST = "https://api.exchange.coinbase.com"
KRAKEN_WS_V2_URL = "wss://ws.kraken.com/v2"
KRAKEN_REST = "https://api.kraken.com/0/public"
KRAKEN_PAIRS = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "DOGE": "DOGEUSD",
}
KRAKEN_WS_SYMBOLS = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "SOL": "SOL/USD",
    "XRP": "XRP/USD",
    "DOGE": "DOGE/USD",
}


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def contract_price_dollars(value):
    price = number(value)
    if price > 1.0:
        price /= 100.0
    return price if 0.0 < price < 1.0 else 0.0


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, float(value)))


def iso_timestamp(value):
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def book_features(bids, asks, depth_bps=10.0):
    bids = [(number(price), number(size)) for price, size, *_rest in (bids or [])]
    asks = [(number(price), number(size)) for price, size, *_rest in (asks or [])]
    bids = [(price, size) for price, size in bids if price > 0 and size > 0]
    asks = [(price, size) for price, size in asks if price > 0 and size > 0]
    if not bids or not asks:
        return {}
    best_bid = max(price for price, _size in bids)
    best_ask = min(price for price, _size in asks)
    mid = (best_bid + best_ask) / 2.0
    if mid <= 0:
        return {}
    band = max(0.00001, depth_bps / 10000.0)
    bid_depth = sum(price * size for price, size in bids if price >= mid * (1.0 - band))
    ask_depth = sum(price * size for price, size in asks if price <= mid * (1.0 + band))
    total_depth = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0
    best_bid_size = sum(size for price, size in bids if price == best_bid)
    best_ask_size = sum(size for price, size in asks if price == best_ask)
    top_total = best_bid_size + best_ask_size
    top_imbalance = (best_bid_size - best_ask_size) / top_total if top_total > 0 else 0.0
    microprice = (
        (best_ask * best_bid_size + best_bid * best_ask_size) / top_total
        if top_total > 0
        else mid
    )
    sorted_bids = sorted(bids, reverse=True)
    sorted_asks = sorted(asks)

    def depth(level_count):
        bid_rows = sorted_bids[:level_count]
        ask_rows = sorted_asks[:level_count]
        bid_usd = sum(price * size for price, size in bid_rows)
        ask_usd = sum(price * size for price, size in ask_rows)
        return bid_usd, ask_usd

    bid_1, ask_1 = depth(1)
    bid_3, ask_3 = depth(3)
    bid_5, ask_5 = depth(5)
    outer_bid = sorted_bids[min(4, len(sorted_bids) - 1)][0]
    outer_ask = sorted_asks[min(4, len(sorted_asks) - 1)][0]
    book_slope_bps = ((outer_ask - outer_bid) - (best_ask - best_bid)) / mid * 10000.0
    inner_depth = bid_1 + ask_1
    outer_depth = max(0.0, bid_5 + ask_5 - inner_depth)
    book_convexity = (
        (outer_depth - inner_depth) / (outer_depth + inner_depth)
        if outer_depth + inner_depth > 0
        else 0.0
    )
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "microprice": microprice,
        "spread_bps": (best_ask - best_bid) / mid * 10000.0,
        "bid_depth_usd": bid_depth,
        "ask_depth_usd": ask_depth,
        "depth_usd": total_depth,
        "book_imbalance": clamp(imbalance),
        "top_imbalance": clamp(top_imbalance),
        "top1_bid_depth_usd": bid_1,
        "top1_ask_depth_usd": ask_1,
        "top1_depth_usd": bid_1 + ask_1,
        "top3_bid_depth_usd": bid_3,
        "top3_ask_depth_usd": ask_3,
        "top3_depth_usd": bid_3 + ask_3,
        "top5_bid_depth_usd": bid_5,
        "top5_ask_depth_usd": ask_5,
        "top5_depth_usd": bid_5 + ask_5,
        "book_slope_bps": book_slope_bps,
        "book_convexity": clamp(book_convexity),
        "microprice_displacement_bps": (microprice - mid) / mid * 10000.0,
    }


def mid_path_features(history, now_ts=None, windows=(5, 15, 30, 60, 180)):
    """Measure the time-aligned path ending at the latest streamed midpoint."""
    now_ts = float(now_ts or time.time())
    rows = sorted(
        (float(ts), number(price))
        for ts, price in history or []
        if number(price) > 0 and float(ts) <= now_ts + 2.0
    )
    if not rows:
        return {}
    latest_ts, latest_price = rows[-1]
    result = {
        "path_latest_ts": latest_ts,
        "path_latest_age_seconds": max(0.0, now_ts - latest_ts),
        "path_observations": len(rows),
    }
    for seconds in windows:
        target = latest_ts - float(seconds)
        prior = min(rows, key=lambda row: abs(row[0] - target))
        usable = abs(prior[0] - target) <= max(2.0, float(seconds) * 0.25)
        result[f"mid_return_{seconds}s_bps"] = (
            math.log(latest_price / prior[1]) * 10000.0
            if usable and prior[1] > 0
            else None
        )
        result[f"mid_return_{seconds}s_observed_span"] = (
            latest_ts - prior[0] if usable else None
        )
    short = result.get("mid_return_15s_bps")
    medium = result.get("mid_return_60s_bps")
    result["path_velocity_15s_bps_per_second"] = (
        short / 15.0 if short is not None else None
    )
    result["path_acceleration_15v60_bps_per_second"] = (
        short / 15.0 - medium / 60.0
        if short is not None and medium is not None
        else None
    )
    return result


def trade_flow_features(trades, now_ts=None):
    now_ts = float(now_ts or time.time())
    windows = (10, 30, 60, 300, 900, 3600)
    signed = defaultdict(float)
    total = defaultdict(float)
    counts = defaultdict(int)
    prices_300s = []
    for trade in trades or []:
        ts = number(trade.get("timestamp"))
        age = now_ts - ts
        if ts <= 0 or age < -2 or age > 3600:
            continue
        notional = max(0.0, number(trade.get("price")) * number(trade.get("size")))
        direction = clamp(trade.get("direction") or 0)
        if age <= 300:
            prices_300s.append((ts, number(trade.get("price"))))
        for window in windows:
            if age <= window:
                signed[window] += direction * notional
                total[window] += notional
                counts[window] += 1
    features = {}
    for window in windows:
        features[f"trade_flow_{window}s"] = clamp(signed[window] / total[window]) if total[window] > 0 else 0.0
        features[f"trade_notional_{window}s"] = total[window]
        features[f"trade_count_{window}s"] = counts[window]
    prices_300s.sort()
    if len(prices_300s) >= 2 and prices_300s[0][1] > 0:
        features["trade_momentum_300s"] = math.log(prices_300s[-1][1] / prices_300s[0][1])
    else:
        features["trade_momentum_300s"] = 0.0
    return features


class CoinbaseMicrostructureStream:
    """Maintain a compact Coinbase L2 book and recent aggressive trade flow."""

    def __init__(self, asset_products):
        self.asset_products = {
            str(asset).upper(): str(product)
            for asset, product in (asset_products or {}).items()
            if asset and product
        }
        self.product_assets = {product: asset for asset, product in self.asset_products.items()}
        self.books = {
            product: {"bids": {}, "asks": {}, "updated_at": 0.0}
            for product in self.product_assets
        }
        # One hour of trades gives the regime layer useful flow context without
        # creating any additional REST/WebSocket subscriptions.
        self.trades = {product: deque(maxlen=20000) for product in self.product_assets}
        self.mid_history = {product: deque(maxlen=7200) for product in self.product_assets}
        self.last_trade_id = {product: 0 for product in self.product_assets}
        self.last_heartbeat_trade_id = {product: 0 for product in self.product_assets}
        self.recent_trade_ids = {product: deque(maxlen=5000) for product in self.product_assets}
        self.recent_trade_id_sets = {product: set() for product in self.product_assets}
        self.gap_backfill_inflight = set()
        self.trade_gap_count = 0
        self.trade_backfill_count = 0
        self.trade_backfill_incomplete_count = 0
        self.lock = threading.Lock()
        self.thread = None
        self.started_at = 0.0
        self.last_message_at = 0.0
        self.last_error = ""
        self.connected = False

    def start(self):
        if self.thread and self.thread.is_alive():
            return True
        if websockets is None or not self.product_assets:
            self.last_error = "websockets_dependency_unavailable"
            return False
        self.thread = threading.Thread(target=self._thread_main, name="coinbase-microstructure", daemon=True)
        self.started_at = time.time()
        self.thread.start()
        return True

    def _thread_main(self):
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.connected = False

    async def _run_forever(self):
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    COINBASE_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=32 * 1024 * 1024,
                ) as websocket:
                    await websocket.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": sorted(self.product_assets),
                        "channels": ["level2_batch", "matches", "heartbeat"],
                    }))
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    async for raw in websocket:
                        self._handle_message(json.loads(raw))
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _trim_book(self, book):
        bids = book["bids"]
        asks = book["asks"]
        if not bids or not asks:
            return
        best_bid = max(bids)
        best_ask = min(asks)
        mid = (best_bid + best_ask) / 2.0
        low, high = mid * 0.995, mid * 1.005
        book["bids"] = {price: size for price, size in bids.items() if price >= low}
        book["asks"] = {price: size for price, size in asks.items() if price <= high}

    def _record_mid_locked(self, product, now_ts):
        book = self.books.get(product) or {}
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}
        if not bids or not asks:
            return
        mid = (max(bids) + min(asks)) / 2.0
        history = self.mid_history[product]
        second = int(now_ts)
        if history and int(history[-1][0]) == second:
            history[-1] = (float(second), mid)
        else:
            history.append((float(second), mid))

    def _append_trade_locked(self, product, row, trade_id=0):
        trade_id = int(number(trade_id))
        if trade_id > 0:
            seen = self.recent_trade_id_sets[product]
            ids = self.recent_trade_ids[product]
            if trade_id in seen:
                return False
            if len(ids) >= ids.maxlen:
                seen.discard(ids[0])
            ids.append(trade_id)
            seen.add(trade_id)
            self.last_trade_id[product] = max(self.last_trade_id[product], trade_id)
        self.trades[product].append(row)
        return True

    def _schedule_trade_backfill(self, product, after_trade_id, heartbeat_trade_id):
        with self.lock:
            if product in self.gap_backfill_inflight:
                return
            self.gap_backfill_inflight.add(product)

        def worker():
            added = 0
            complete = False
            try:
                payload = request_json(
                    f"{COINBASE_REST}/products/{quote(product)}/trades?limit=100",
                    timeout=5,
                )
                rows = []
                for item in payload if isinstance(payload, list) else []:
                    trade_id = int(number(item.get("trade_id")))
                    if trade_id <= int(after_trade_id) or trade_id > int(heartbeat_trade_id):
                        continue
                    maker_side = str(item.get("side") or "").lower()
                    rows.append((trade_id, {
                        "timestamp": iso_timestamp(item.get("time")) or time.time(),
                        "price": number(item.get("price")),
                        "size": number(item.get("size")),
                        "direction": 1.0 if maker_side == "sell" else -1.0 if maker_side == "buy" else 0.0,
                    }))
                with self.lock:
                    for trade_id, row in sorted(rows):
                        added += int(self._append_trade_locked(product, row, trade_id=trade_id))
                    expected = max(0, int(heartbeat_trade_id) - int(after_trade_id))
                    complete = (
                        expected <= 100
                        and self.last_trade_id[product] >= int(heartbeat_trade_id)
                        and added >= expected
                    )
                    self.trade_backfill_count += added
                    if not complete:
                        self.trade_backfill_incomplete_count += 1
                        self.last_error = (
                            f"coinbase_trade_gap_incomplete product={product} "
                            f"after={after_trade_id} heartbeat={heartbeat_trade_id}"
                        )
            except Exception as exc:
                with self.lock:
                    self.trade_backfill_incomplete_count += 1
                    self.last_error = f"coinbase_trade_backfill:{type(exc).__name__}:{exc}"
            finally:
                with self.lock:
                    self.gap_backfill_inflight.discard(product)

        threading.Thread(
            target=worker,
            name=f"coinbase-trade-backfill-{product}",
            daemon=True,
        ).start()

    def _handle_message(self, message):
        message_type = str(message.get("type") or "")
        product = str(message.get("product_id") or "")
        if product not in self.product_assets:
            return
        now_ts = time.time()
        gap_request = None
        with self.lock:
            if message_type == "snapshot":
                bids = {
                    number(row[0]): number(row[1])
                    for row in message.get("bids") or []
                    if len(row) >= 2 and number(row[0]) > 0 and number(row[1]) > 0
                }
                asks = {
                    number(row[0]): number(row[1])
                    for row in message.get("asks") or []
                    if len(row) >= 2 and number(row[0]) > 0 and number(row[1]) > 0
                }
                self.books[product] = {"bids": bids, "asks": asks, "updated_at": now_ts}
                self._trim_book(self.books[product])
                self._record_mid_locked(product, now_ts)
            elif message_type == "l2update":
                book = self.books[product]
                for side, price_value, size_value in message.get("changes") or []:
                    price, size = number(price_value), number(size_value)
                    levels = book["bids"] if side == "buy" else book["asks"]
                    if size <= 0:
                        levels.pop(price, None)
                    elif price > 0:
                        levels[price] = size
                book["updated_at"] = now_ts
                if len(book["bids"]) + len(book["asks"]) > 10000:
                    self._trim_book(book)
                self._record_mid_locked(product, now_ts)
            elif message_type in {"match", "last_match"}:
                # Coinbase reports the maker side. A sell maker means an
                # aggressive buyer crossed the spread, which is positive flow.
                maker_side = str(message.get("side") or "").lower()
                direction = 1.0 if maker_side == "sell" else -1.0 if maker_side == "buy" else 0.0
                self._append_trade_locked(product, {
                    "timestamp": iso_timestamp(message.get("time")) or now_ts,
                    "price": number(message.get("price")),
                    "size": number(message.get("size")),
                    "direction": direction,
                }, trade_id=message.get("trade_id"))
            elif message_type == "heartbeat":
                heartbeat_trade_id = int(number(message.get("last_trade_id")))
                self.last_heartbeat_trade_id[product] = max(
                    self.last_heartbeat_trade_id[product],
                    heartbeat_trade_id,
                )
                last_seen = self.last_trade_id[product]
                if last_seen <= 0 and heartbeat_trade_id > 0:
                    # Establish the subscription baseline; only subsequent
                    # heartbeat jumps represent messages that could be lost.
                    self.last_trade_id[product] = heartbeat_trade_id
                elif (
                    heartbeat_trade_id > last_seen + 1
                    and product not in self.gap_backfill_inflight
                ):
                    self.trade_gap_count += 1
                    gap_request = (product, last_seen, heartbeat_trade_id)
            self.last_message_at = now_ts
        if gap_request:
            self._schedule_trade_backfill(*gap_request)

    def snapshot(self, asset):
        asset = str(asset or "").upper()
        product = self.asset_products.get(asset)
        if not product:
            return {}
        now_ts = time.time()
        with self.lock:
            book = self.books.get(product) or {}
            bids = list((book.get("bids") or {}).items())
            asks = list((book.get("asks") or {}).items())
            trades = list(self.trades.get(product) or ())
            updated_at = number(book.get("updated_at"))
            last_message_at = self.last_message_at
            error = self.last_error
            connected = self.connected
            last_trade_id = self.last_trade_id.get(product, 0)
            heartbeat_trade_id = self.last_heartbeat_trade_id.get(product, 0)
            mid_history = list(self.mid_history.get(product) or ())
        result = {
            **book_features(bids, asks),
            **trade_flow_features(trades, now_ts=now_ts),
            **mid_path_features(mid_history, now_ts=now_ts),
            "source": "coinbase_ws",
            "connected": connected,
            "book_age_seconds": round(max(0.0, now_ts - updated_at), 3) if updated_at else None,
            "stream_age_seconds": round(max(0.0, now_ts - last_message_at), 3) if last_message_at else None,
            "last_trade_id": last_trade_id,
            "heartbeat_trade_id": heartbeat_trade_id,
            "trade_gap_count": self.trade_gap_count,
            "trade_backfill_count": self.trade_backfill_count,
            "trade_backfill_incomplete_count": self.trade_backfill_incomplete_count,
        }
        if error:
            result["error"] = error
        return result

    def settlement_window(self, asset, close_time, window_seconds=60.0):
        asset = str(asset or "").upper()
        product = self.asset_products.get(asset)
        close_ts = iso_timestamp(close_time) if not isinstance(close_time, (int, float)) else float(close_time)
        if not product or close_ts <= 0:
            return {}
        start_ts = close_ts - max(1.0, number(window_seconds, 60.0))
        with self.lock:
            values = [price for ts, price in self.mid_history.get(product, ()) if start_ts <= ts <= close_ts]
        return {
            "source": "coinbase_ws_one_second_mid",
            "asset": asset,
            "close_time": datetime.fromtimestamp(close_ts, tz=timezone.utc).isoformat(),
            "window_seconds": round(max(1.0, number(window_seconds, 60.0)), 3),
            "observations": len(values),
            "average": sum(values) / len(values) if values else None,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
        }


class KrakenMicrostructureStream:
    """Maintain Kraken WebSocket v2 L2 books, trades, and settlement-window mids."""

    def __init__(self, assets):
        self.asset_symbols = {
            str(asset).upper(): KRAKEN_WS_SYMBOLS[str(asset).upper()]
            for asset in assets or []
            if str(asset).upper() in KRAKEN_WS_SYMBOLS
        }
        self.symbol_assets = {symbol: asset for asset, symbol in self.asset_symbols.items()}
        self.books = {symbol: {"bids": {}, "asks": {}, "updated_at": 0.0} for symbol in self.symbol_assets}
        self.trades = {symbol: deque(maxlen=20000) for symbol in self.symbol_assets}
        self.mid_history = {symbol: deque(maxlen=7200) for symbol in self.symbol_assets}
        self.lock = threading.Lock()
        self.thread = None
        self.connected = False
        self.last_message_at = 0.0
        self.last_error = ""
        self.reconnect_count = 0
        self.checksum_failure_count = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return True
        if websockets is None or not self.symbol_assets:
            self.last_error = "websockets_dependency_unavailable"
            return False
        self.thread = threading.Thread(target=self._thread_main, name="kraken-microstructure", daemon=True)
        self.thread.start()
        return True

    def _thread_main(self):
        try:
            asyncio.run(self._run_forever())
        except Exception as exc:
            self.connected = False
            self.last_error = f"{type(exc).__name__}: {exc}"

    async def _run_forever(self):
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                    KRAKEN_WS_V2_URL,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=32 * 1024 * 1024,
                ) as websocket:
                    symbols = sorted(self.symbol_assets)
                    await websocket.send(json.dumps({
                        "method": "subscribe",
                        "params": {"channel": "book", "symbol": symbols, "depth": 25, "snapshot": True},
                    }))
                    await websocket.send(json.dumps({
                        "method": "subscribe",
                        "params": {"channel": "trade", "symbol": symbols, "snapshot": True},
                    }))
                    self.connected = True
                    self.last_error = ""
                    backoff = 1.0
                    async for raw in websocket:
                        self._handle_message(json.loads(raw, parse_float=Decimal))
            except Exception as exc:
                self.connected = False
                self.reconnect_count += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _record_mid_locked(self, symbol, now_ts):
        book = self.books.get(symbol) or {}
        bids = book.get("bids") or {}
        asks = book.get("asks") or {}
        if not bids or not asks:
            return
        mid = float((max(bids) + min(asks)) / Decimal("2"))
        history = self.mid_history[symbol]
        second = int(now_ts)
        if history and int(history[-1][0]) == second:
            history[-1] = (float(second), mid)
        else:
            history.append((float(second), mid))

    @staticmethod
    def _decimal(value):
        try:
            return value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    @staticmethod
    def _checksum_component(value):
        text = format(value, "f").replace(".", "").lstrip("0")
        return text or "0"

    def _book_checksum_locked(self, symbol):
        book = self.books.get(symbol) or {}
        asks = sorted((book.get("asks") or {}).items())[:10]
        bids = sorted((book.get("bids") or {}).items(), reverse=True)[:10]
        payload = "".join(
            self._checksum_component(price) + self._checksum_component(quantity)
            for price, quantity in [*asks, *bids]
        )
        return zlib.crc32(payload.encode("utf-8")) & 0xFFFFFFFF

    def _handle_message(self, message):
        channel = str(message.get("channel") or "")
        if channel not in {"book", "trade"}:
            if message.get("success") is False:
                self.last_error = str(message.get("error") or message)[:500]
            return
        message_type = str(message.get("type") or "update")
        now_ts = time.time()
        with self.lock:
            for row in message.get("data") or []:
                symbol = str(row.get("symbol") or "")
                if symbol not in self.symbol_assets:
                    continue
                if channel == "book":
                    book = self.books[symbol]
                    if message_type == "snapshot":
                        book["bids"] = {}
                        book["asks"] = {}
                    for key in ("bids", "asks"):
                        levels = book[key]
                        for level in row.get(key) or []:
                            price = self._decimal(level.get("price"))
                            quantity = self._decimal(level.get("qty"))
                            if quantity <= 0:
                                levels.pop(price, None)
                            elif price > 0:
                                levels[price] = quantity
                    book["bids"] = dict(sorted(book["bids"].items(), reverse=True)[:25])
                    book["asks"] = dict(sorted(book["asks"].items())[:25])
                    expected_checksum = row.get("checksum")
                    if expected_checksum is not None:
                        actual_checksum = self._book_checksum_locked(symbol)
                        if actual_checksum != int(expected_checksum):
                            self.checksum_failure_count += 1
                            self.last_error = (
                                f"kraken_book_checksum_mismatch symbol={symbol} "
                                f"expected={expected_checksum} actual={actual_checksum}"
                            )
                            self.books[symbol] = {"bids": {}, "asks": {}, "updated_at": 0.0}
                            raise RuntimeError(self.last_error)
                    book["updated_at"] = now_ts
                    self._record_mid_locked(symbol, now_ts)
                else:
                    trade_rows = row.get("trades") if isinstance(row.get("trades"), list) else [row]
                    for trade in trade_rows:
                        side = str(trade.get("side") or "").lower()
                        self.trades[symbol].append({
                            "timestamp": iso_timestamp(trade.get("timestamp")) or now_ts,
                            "price": number(trade.get("price")),
                            "size": number(trade.get("qty")),
                            "direction": 1.0 if side == "buy" else -1.0 if side == "sell" else 0.0,
                        })
            self.last_message_at = now_ts

    def snapshot(self, asset):
        asset = str(asset or "").upper()
        symbol = self.asset_symbols.get(asset)
        if not symbol:
            return {}
        now_ts = time.time()
        with self.lock:
            book = self.books.get(symbol) or {}
            bids = list((book.get("bids") or {}).items())
            asks = list((book.get("asks") or {}).items())
            trades = list(self.trades.get(symbol) or ())
            updated_at = number(book.get("updated_at"))
            connected = self.connected
            error = self.last_error
            last_message_at = self.last_message_at
            mid_history = list(self.mid_history.get(symbol) or ())
        result = {
            **book_features(bids, asks),
            **trade_flow_features(trades, now_ts=now_ts),
            **mid_path_features(mid_history, now_ts=now_ts),
            "source": "kraken_ws_v2",
            "connected": connected,
            "book_age_seconds": round(max(0.0, now_ts - updated_at), 3) if updated_at else None,
            "stream_age_seconds": round(max(0.0, now_ts - last_message_at), 3) if last_message_at else None,
            "reconnect_count": self.reconnect_count,
            "checksum_failure_count": self.checksum_failure_count,
        }
        if error:
            result["error"] = error
        return result

    def settlement_window(self, asset, close_time, window_seconds=60.0):
        asset = str(asset or "").upper()
        symbol = self.asset_symbols.get(asset)
        close_ts = iso_timestamp(close_time) if not isinstance(close_time, (int, float)) else float(close_time)
        if not symbol or close_ts <= 0:
            return {}
        start_ts = close_ts - max(1.0, number(window_seconds, 60.0))
        with self.lock:
            values = [price for ts, price in self.mid_history.get(symbol, ()) if start_ts <= ts <= close_ts]
        return {
            "source": "kraken_ws_v2_one_second_mid",
            "asset": asset,
            "close_time": datetime.fromtimestamp(close_ts, tz=timezone.utc).isoformat(),
            "window_seconds": round(max(1.0, number(window_seconds, 60.0)), 3),
            "observations": len(values),
            "average": sum(values) / len(values) if values else None,
            "minimum": min(values) if values else None,
            "maximum": max(values) if values else None,
        }


def request_json(url, timeout=5):
    response = requests.get(url, timeout=timeout, headers={"User-Agent": "polymaker-crypto-microstructure/1.0"})
    response.raise_for_status()
    return response.json()


def coinbase_rest_snapshot(product, timeout=5):
    book = request_json(f"{COINBASE_REST}/products/{quote(product)}/book?level=1", timeout=timeout)
    trades_payload = request_json(f"{COINBASE_REST}/products/{quote(product)}/trades?limit=100", timeout=timeout)
    trades = []
    for row in trades_payload if isinstance(trades_payload, list) else []:
        maker_side = str(row.get("side") or "").lower()
        trades.append({
            "timestamp": iso_timestamp(row.get("time")),
            "price": number(row.get("price")),
            "size": number(row.get("size")),
            "direction": 1.0 if maker_side == "sell" else -1.0 if maker_side == "buy" else 0.0,
        })
    return {
        **book_features(book.get("bids") or [], book.get("asks") or []),
        **trade_flow_features(trades),
        "source": "coinbase_rest_l1",
        "connected": True,
        "book_age_seconds": 0.0,
    }


def kraken_rest_snapshot(asset, timeout=5):
    pair = KRAKEN_PAIRS.get(str(asset or "").upper())
    if not pair:
        return {}
    depth_payload = request_json(f"{KRAKEN_REST}/Depth?pair={quote(pair)}&count=25", timeout=timeout)
    trade_payload = request_json(f"{KRAKEN_REST}/Trades?pair={quote(pair)}&count=100", timeout=timeout)
    if depth_payload.get("error"):
        raise RuntimeError(",".join(depth_payload["error"]))
    if trade_payload.get("error"):
        raise RuntimeError(",".join(trade_payload["error"]))
    depth_result = depth_payload.get("result") or {}
    trade_result = trade_payload.get("result") or {}
    depth_row = next(iter(depth_result.values()), {})
    raw_trades = next((value for value in trade_result.values() if isinstance(value, list)), [])
    trades = []
    for row in raw_trades:
        if len(row) < 4:
            continue
        # Kraken uses b/s for the taker direction.
        direction = 1.0 if str(row[3]).lower() == "b" else -1.0 if str(row[3]).lower() == "s" else 0.0
        trades.append({
            "timestamp": number(row[2]),
            "price": number(row[0]),
            "size": number(row[1]),
            "direction": direction,
        })
    return {
        **book_features(depth_row.get("bids") or [], depth_row.get("asks") or []),
        **trade_flow_features(trades),
        "source": "kraken_rest_l2",
        "connected": True,
        "book_age_seconds": 0.0,
    }


def combined_asset_microstructure(
    asset,
    coinbase_product,
    coinbase_stream=None,
    kraken_stream=None,
    timeout=5,
):
    errors = []
    coinbase = coinbase_stream.snapshot(asset) if coinbase_stream else {}
    if not coinbase.get("mid") or number(coinbase.get("book_age_seconds"), 999) > 15:
        try:
            coinbase = coinbase_rest_snapshot(coinbase_product, timeout=timeout)
        except Exception as exc:
            errors.append(f"coinbase:{type(exc).__name__}:{exc}")
            coinbase = {}
    kraken = kraken_stream.snapshot(asset) if kraken_stream else {}
    if not kraken.get("mid") or number(kraken.get("book_age_seconds"), 999) > 15:
        try:
            kraken = kraken_rest_snapshot(asset, timeout=timeout)
        except Exception as exc:
            errors.append(f"kraken:{type(exc).__name__}:{exc}")
            kraken = {}

    sources = [row for row in (coinbase, kraken) if number(row.get("mid")) > 0]
    total_weight = sum(max(1.0, math.sqrt(max(0.0, number(row.get("depth_usd"))))) for row in sources)
    composite_mid = (
        sum(
            number(row.get("mid")) * max(1.0, math.sqrt(max(0.0, number(row.get("depth_usd")))))
            for row in sources
        ) / total_weight
        if total_weight > 0
        else 0.0
    )
    mids = [number(row.get("mid")) for row in sources]
    dispersion_bps = (
        (max(mids) - min(mids)) / composite_mid * 10000.0
        if len(mids) >= 2 and composite_mid > 0
        else 0.0
    )
    source_scores = {}
    for row in sources:
        score = (
            0.45 * number(row.get("book_imbalance"))
            + 0.15 * number(row.get("top_imbalance"))
            + 0.40 * number(row.get("trade_flow_60s"))
        )
        source_scores[str(row.get("source") or f"source_{len(source_scores) + 1}")] = clamp(score)
    score_values = list(source_scores.values())
    flow_score = sum(score_values) / len(score_values) if score_values else 0.0
    flow_agreement = (
        len(score_values) >= 2
        and (
            all(score >= 0 for score in score_values)
            or all(score <= 0 for score in score_values)
        )
    )
    return {
        "asset": str(asset or "").upper(),
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coinbase": coinbase,
        "kraken": kraken,
        "settlement_proxy_spot": composite_mid,
        "settlement_proxy_source_count": len(sources),
        "cross_exchange_dispersion_bps": dispersion_bps,
        "underlying_flow_score": clamp(flow_score),
        "underlying_flow_strength": abs(clamp(flow_score)),
        "underlying_flow_agreement": bool(flow_agreement),
        "source_flow_scores": source_scores,
        "errors": errors,
    }


def kalshi_market_microstructure(base_url, ticker, timeout=5):
    encoded_ticker = quote(str(ticker or ""), safe="")
    orderbook_payload = request_json(f"{base_url}/markets/{encoded_ticker}/orderbook", timeout=timeout)
    # Capture the executable-book time before making the optional trades call.
    # The trades endpoint is more rate-limit prone and must not make a valid
    # order book unusable for pricing.
    orderbook_fetched_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    trades_payload = {"trades": []}
    trades_fetched_at = None
    trades_error = None
    try:
        trades_payload = request_json(
            f"{base_url}/markets/trades?ticker={encoded_ticker}&limit=200&min_ts={int(time.time()) - 300}",
            timeout=timeout,
        )
        trades_fetched_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    except Exception as exc:
        trades_error = f"{type(exc).__name__}:{exc}"
    orderbook = orderbook_payload.get("orderbook_fp") or orderbook_payload.get("orderbook") or {}
    yes_levels = orderbook.get("yes_dollars") or orderbook.get("yes") or []
    no_levels = orderbook.get("no_dollars") or orderbook.get("no") or []
    yes_depth = sum(contract_price_dollars(price) * number(size) for price, size, *_rest in yes_levels)
    no_depth = sum(contract_price_dollars(price) * number(size) for price, size, *_rest in no_levels)
    yes_bids = [
        contract_price_dollars(price)
        for price, size, *_rest in yes_levels
        if number(size) > 0 and contract_price_dollars(price) > 0
    ]
    no_bids = [
        contract_price_dollars(price)
        for price, size, *_rest in no_levels
        if number(size) > 0 and contract_price_dollars(price) > 0
    ]
    best_yes_bid = max(yes_bids) if yes_bids else 0.0
    best_no_bid = max(no_bids) if no_bids else 0.0
    # Directional book imbalance must compare contracts, not price-weighted
    # dollars. Price weighting turns the feature into a near-duplicate of the
    # market probability even when both sides display identical quantities.
    yes_quantity_levels = sorted(
        ((contract_price_dollars(price), number(size)) for price, size, *_rest in yes_levels if number(size) > 0),
        reverse=True,
    )[:5]
    no_quantity_levels = sorted(
        ((contract_price_dollars(price), number(size)) for price, size, *_rest in no_levels if number(size) > 0),
        reverse=True,
    )[:5]
    yes_quantity = sum(size for _price, size in yes_quantity_levels)
    no_quantity = sum(size for _price, size in no_quantity_levels)
    total_quantity = yes_quantity + no_quantity
    book_imbalance = (
        (yes_quantity - no_quantity) / total_quantity
        if total_quantity > 0
        else 0.0
    )
    best_yes_size = sum(
        number(size)
        for price, size, *_rest in yes_levels
        if best_yes_bid > 0 and abs(contract_price_dollars(price) - best_yes_bid) < 1e-9
    )
    best_no_size = sum(
        number(size)
        for price, size, *_rest in no_levels
        if best_no_bid > 0 and abs(contract_price_dollars(price) - best_no_bid) < 1e-9
    )
    yes_ask = 1.0 - best_no_bid if best_no_bid else 0.0
    microprice_yes = (
        (yes_ask * best_yes_size + best_yes_bid * best_no_size)
        / (best_yes_size + best_no_size)
        if best_yes_bid and yes_ask and best_yes_size + best_no_size > 0
        else 0.0
    )
    trades = []
    for row in trades_payload.get("trades") or []:
        side = str(row.get("taker_outcome_side") or row.get("taker_side") or "").lower()
        yes_price = number(row.get("yes_price_dollars") or row.get("yes_price"))
        if yes_price > 1:
            yes_price /= 100.0
        count = number(row.get("count_fp") or row.get("count"))
        trades.append({
            "timestamp": iso_timestamp(row.get("created_time")),
            "price": yes_price,
            "size": count,
            "direction": 1.0 if side == "yes" else -1.0 if side == "no" else 0.0,
        })
    flow = trade_flow_features(trades)
    latest_trade = max(trades, key=lambda row: number(row.get("timestamp")), default={})
    return {
        "source": "kalshi_rest_orderbook_trades",
        "orderbook_fetched_at": orderbook_fetched_at,
        "trades_fetched_at": trades_fetched_at,
        "trades_error": trades_error,
        "best_yes_bid_cents": round(best_yes_bid * 100.0, 4) if best_yes_bid else None,
        "best_no_bid_cents": round(best_no_bid * 100.0, 4) if best_no_bid else None,
        "best_yes_entry_price_cents": (
            round((1.0 - best_no_bid) * 100.0, 4)
            if best_no_bid
            else None
        ),
        "best_no_entry_price_cents": (
            round((1.0 - best_yes_bid) * 100.0, 4)
            if best_yes_bid
            else None
        ),
        "yes_depth_dollars": yes_depth,
        "no_depth_dollars": no_depth,
        "yes_depth_contracts": yes_quantity,
        "no_depth_contracts": no_quantity,
        "book_imbalance": clamp(book_imbalance),
        "microprice_yes_cents": round(microprice_yes * 100.0, 4) if microprice_yes else None,
        "yes_levels": [
            [round(price * 100.0, 4), round(size, 6)]
            for price, size in sorted(yes_quantity_levels, reverse=True)
        ],
        "no_levels": [
            [round(price * 100.0, 4), round(size, 6)]
            for price, size in sorted(no_quantity_levels, reverse=True)
        ],
        "spread_yes_cents": round((yes_ask - best_yes_bid) * 100.0, 4)
        if best_yes_bid and yes_ask
        else None,
        "top_imbalance": clamp(
            (best_yes_size - best_no_size) / (best_yes_size + best_no_size)
        ) if best_yes_size + best_no_size > 0 else 0.0,
        "top1_depth_contracts": round(best_yes_size + best_no_size, 4),
        "top3_depth_contracts": round(
            sum(size for _price, size in yes_quantity_levels[:3])
            + sum(size for _price, size in no_quantity_levels[:3]),
            4,
        ),
        "top5_depth_contracts": round(total_quantity, 4),
        "microprice_displacement_yes_cents": round(
            (microprice_yes - (best_yes_bid + yes_ask) / 2.0) * 100.0,
            4,
        ) if microprice_yes and best_yes_bid and yes_ask else None,
        "sequence_valid": True,
        "last_trade_price_cents": (
            round(number(latest_trade.get("price")) * 100.0, 4)
            if latest_trade
            else None
        ),
        "last_trade_ts": latest_trade.get("timestamp") if latest_trade else None,
        **flow,
    }


def combined_flow_review(underlying, kalshi, *, include_kalshi_book_vote=False):
    underlying_score = number((underlying or {}).get("underlying_flow_score"))
    kalshi_book = number((kalshi or {}).get("book_imbalance"))
    kalshi_trades = number((kalshi or {}).get("trade_flow_60s"))
    # Kalshi trade flow is an independent execution signal. Corrected contract
    # quantity imbalance remains diagnostic by default until its forward shadow
    # record is large enough to count as another directional vote.
    score = clamp(0.75 * underlying_score + 0.25 * kalshi_trades)
    source_scores = {
        str(name): clamp(value)
        for name, value in ((underlying or {}).get("source_flow_scores") or {}).items()
    }
    if kalshi and (
        number(kalshi.get("trade_count_60s")) > 0
        or abs(kalshi_trades) >= 0.05
    ):
        source_scores["kalshi_trades"] = clamp(kalshi_trades)
    if kalshi and include_kalshi_book_vote:
        source_scores["kalshi_book_quantity"] = clamp(kalshi_book)
    minimum_directional_strength = 0.05
    yes_sources = sum(1 for value in source_scores.values() if value >= minimum_directional_strength)
    no_sources = sum(1 for value in source_scores.values() if value <= -minimum_directional_strength)
    direction = "yes" if score > 0 else "no" if score < 0 else "neutral"
    confirming_source_count = (
        yes_sources if direction == "yes"
        else no_sources if direction == "no"
        else 0
    )
    return {
        "score": score,
        "strength": abs(score),
        "direction": direction,
        "source_count": len(source_scores),
        "confirming_source_count": confirming_source_count,
        "yes_source_count": yes_sources,
        "no_source_count": no_sources,
        "minimum_directional_source_strength": minimum_directional_strength,
        "source_scores": source_scores,
        "healthy_source_count": len(source_scores),
        "underlying_score": underlying_score,
        "kalshi_book_imbalance": kalshi_book,
        "kalshi_trade_flow_60s": kalshi_trades,
        "cross_exchange_dispersion_bps": number((underlying or {}).get("cross_exchange_dispersion_bps")),
        "underlying_source_agreement": bool((underlying or {}).get("underlying_flow_agreement")),
    }
