"""Low-latency Kalshi market-data stream for crypto execution and pricing."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


DEFAULT_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
SIZE_EPSILON = 1e-9


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _signed_headers(ws_url, api_key_id, private_key_path="", private_key_pem=""):
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    key_bytes = b""
    path = Path(str(private_key_path or "").strip()) if private_key_path else None
    pem_text = str(private_key_pem or "").strip()
    if path and path.exists():
        key_bytes = path.read_bytes()
    elif pem_text and "BEGIN" in pem_text and "PRIVATE KEY" in pem_text:
        key_bytes = pem_text.encode("utf-8")
    elif pem_text and Path(pem_text).exists():
        key_bytes = Path(pem_text).read_bytes()
    else:
        raise RuntimeError("kalshi_websocket_private_key_missing")
    private_key = serialization.load_pem_private_key(
        key_bytes,
        password=None,
        backend=default_backend(),
    )
    timestamp = str(int(time.time() * 1000))
    sign_path = urlparse(ws_url).path
    message = f"{timestamp}GET{sign_path}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": str(api_key_id or ""),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


class KalshiCryptoStream:
    """Maintain sequence-checked books, ticker quotes, and public trades."""

    def __init__(
        self,
        *,
        api_key_id,
        private_key_path="",
        private_key_pem="",
        ws_url=DEFAULT_WS_URL,
        max_tickers=100,
    ):
        self.api_key_id = str(api_key_id or "")
        self.private_key_path = str(private_key_path or "")
        self.private_key_pem = str(private_key_pem or "")
        self.ws_url = str(ws_url or DEFAULT_WS_URL)
        self.max_tickers = max(1, int(max_tickers))
        self._lock = threading.RLock()
        self._desired = set()
        self._subscribed = set()
        self._snapshots = {}
        self._books = {}
        self._trades = defaultdict(lambda: deque(maxlen=2000))
        self._market_history = defaultdict(lambda: deque(maxlen=7200))
        self._cfbenchmarks = {}
        self._cfbenchmark_history = defaultdict(lambda: deque(maxlen=7200))
        self._cfbenchmark_window_state = {}
        self._cfbenchmark_snapshot_tickers = set()
        self._sequence_by_sid = {}
        self._thread = None
        self._stop = threading.Event()
        self._reconnect_requested = threading.Event()
        self._connected = False
        self._last_error = ""
        self._last_message_at = ""
        self._connection_count = 0
        self._sequence_gap_count = 0
        self._book_inconsistency_count = 0
        self._cfbenchmark_sequence_gap_count = 0
        self._cfbenchmark_subscribed = False
        self._message_id = 1

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        if not self.api_key_id:
            self._last_error = "kalshi_websocket_api_key_missing"
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="kalshi-crypto-stream",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def replace_subscriptions(self, tickers):
        replacement = []
        seen = set()
        for value in tickers or []:
            ticker = str(value or "").strip()
            if not ticker or ticker in seen:
                continue
            replacement.append(ticker)
            seen.add(ticker)
            if len(replacement) >= self.max_tickers:
                break
        with self._lock:
            changed = set(replacement) != self._desired
            self._desired = set(replacement)
            connected = self._connected
        if changed and connected:
            self._reconnect_requested.set()

    def wait_for_fresh(self, tickers, *, max_age_seconds=2.0, timeout_seconds=1.5):
        requested = list(dict.fromkeys(str(value or "").strip() for value in tickers or [] if str(value or "").strip()))
        deadline = time.time() + max(0.0, _number(timeout_seconds))
        fresh = []
        while requested:
            fresh = [ticker for ticker in requested if self.snapshot(ticker, max_age_seconds=max_age_seconds).get("fresh")]
            if len(fresh) == len(requested) or time.time() >= deadline:
                break
            time.sleep(0.05)
        return {
            "requested_tickers": len(requested),
            "fresh_tickers": len(fresh),
            "missing_or_stale_tickers": max(0, len(requested) - len(fresh)),
        }

    def snapshot(self, ticker, max_age_seconds=2.0):
        with self._lock:
            row = dict(self._snapshots.get(str(ticker or "")) or {})
        if not row:
            return {}
        # Trades and ticker messages are not evidence that depth is current.
        row.pop("received_unix", None)
        received_unix = _number(row.get("book_received_unix", 0.0))
        age = max(0.0, time.time() - received_unix) if received_unix else float("inf")
        row["age_seconds"] = round(age, 3) if age != float("inf") else None
        row["fresh"] = bool(
            age <= max(0.1, _number(max_age_seconds, 2.0))
            and row.get("sequence_valid", False)
            and row.get("book_consistent", False)
            and row.get("book_snapshot_valid", False)
        )
        return row

    def orderbook_payload(self, ticker, max_age_seconds=2.0):
        snapshot = self.snapshot(ticker, max_age_seconds=max_age_seconds)
        if not snapshot.get("fresh"):
            return None
        yes = snapshot.get("yes_levels") or []
        no = snapshot.get("no_levels") or []
        if not yes or not no:
            return None
        return {
            "orderbook_fp": {
                "yes_dollars": [[f"{price / 100.0:.4f}", f"{size:.2f}"] for price, size in yes],
                "no_dollars": [[f"{price / 100.0:.4f}", f"{size:.2f}"] for price, size in no],
            },
            "source": "kalshi_websocket",
            "received_at": snapshot.get("book_received_at"),
            "age_seconds": snapshot.get("age_seconds"),
            "book_revision": snapshot.get("book_revision"),
            "sequence_valid": snapshot.get("sequence_valid", False),
        }

    def _invalidate_books(self):
        """A new connection/sequence epoch requires a new full snapshot."""
        with self._lock:
            self._books.clear()
            for row in self._snapshots.values():
                row.update({
                    "book_snapshot_valid": False, "sequence_valid": False,
                    "book_consistent": False, "yes_levels": [], "no_levels": [],
                    "best_yes_bid_cents": None, "best_no_bid_cents": None,
                    "best_yes_entry_price_cents": None, "best_no_entry_price_cents": None,
                    "book_received_unix": 0.0,
                })

    def status(self):
        with self._lock:
            return {
                "enabled": True,
                "connected": self._connected,
                "desired_tickers": len(self._desired),
                "subscribed_tickers": len(self._subscribed),
                "snapshot_count": len(self._snapshots),
                "last_message_at": self._last_message_at,
                "last_error": self._last_error,
                "connection_count": self._connection_count,
                "sequence_gap_count": self._sequence_gap_count,
                "book_inconsistency_count": self._book_inconsistency_count,
                "orderbook_price_convention": "yes_price",
                "cfbenchmark_sequence_gap_count": self._cfbenchmark_sequence_gap_count,
                "cfbenchmark_subscribed": self._cfbenchmark_subscribed,
                "cfbenchmark_indices": sorted(self._cfbenchmarks),
                "thread_alive": bool(self._thread and self._thread.is_alive()),
            }

    def cfbenchmark_snapshot(self, index_id="BRTI", max_age_seconds=2.0):
        with self._lock:
            row = dict(self._cfbenchmarks.get(str(index_id or "").upper()) or {})
        if not row:
            return {}
        received_unix = _number(row.pop("received_unix", 0.0))
        age = max(0.0, time.time() - received_unix) if received_unix else float("inf")
        row["age_seconds"] = round(age, 3) if age != float("inf") else None
        row["fresh"] = bool(
            age <= max(0.1, _number(max_age_seconds, 2.0))
            and row.get("sequence_valid", True)
            and row.get("window_integrity", True)
        )
        return row

    def cfbenchmark_history(self, index_id="BRTI", seconds=300.0):
        cutoff = time.time() - max(1.0, _number(seconds, 300.0))
        with self._lock:
            rows = list(self._cfbenchmark_history.get(str(index_id or "").upper()) or ())
        return [dict(row) for row in rows if _number(row.get("received_unix")) >= cutoff]

    def replace_cfbenchmark_snapshot_tickers(self, tickers):
        with self._lock:
            self._cfbenchmark_snapshot_tickers = {
                str(value or "").strip() for value in tickers or [] if str(value or "").strip()
            }

    def _thread_main(self):
        asyncio.run(self._run_forever())

    async def _run_forever(self):
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._invalidate_books()
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                await asyncio.sleep(2.0)

    async def _connect_once(self):
        import websockets

        headers = _signed_headers(
            self.ws_url,
            self.api_key_id,
            self.private_key_path,
            self.private_key_pem,
        )
        async with websockets.connect(
            self.ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_queue=4096,
        ) as websocket:
            with self._lock:
                self._connected = True
                self._subscribed.clear()
                self._sequence_by_sid.clear()
                self._invalidate_books()
                self._cfbenchmark_subscribed = False
                for index_id, state in list(self._cfbenchmark_window_state.items()):
                    if 0 < int(_number(state.get("window_size"), 0.0)) < 60:
                        state = dict(state)
                        state["window_integrity"] = False
                        self._cfbenchmark_window_state[index_id] = state
                        current = dict(self._cfbenchmarks.get(index_id) or {})
                        current["window_integrity"] = False
                        self._cfbenchmarks[index_id] = current
                self._connection_count += 1
                self._last_error = ""
            self._reconnect_requested.clear()
            while not self._stop.is_set():
                if self._reconnect_requested.is_set():
                    return
                await self._send_pending_subscriptions(websocket)
                try:
                    raw = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._handle_message(raw)

    async def _send_pending_subscriptions(self, websocket):
        with self._lock:
            cfbenchmark_pending = not self._cfbenchmark_subscribed
        if cfbenchmark_pending:
            message_id = self._message_id
            self._message_id += 1
            await websocket.send(json.dumps({
                "id": message_id,
                "cmd": "subscribe",
                "params": {
                    "channels": ["cfbenchmarks_value"],
                    "index_ids": ["BRTI"],
                },
            }))
            with self._lock:
                self._cfbenchmark_subscribed = True
        with self._lock:
            pending = sorted(self._desired - self._subscribed)
        for offset in range(0, len(pending), 100):
            batch = pending[offset : offset + 100]
            if not batch:
                continue
            subscriptions = [
                {
                    "channels": ["orderbook_delta"],
                    "market_tickers": batch,
                    # Pin the unified YES-price convention.  NO-side prices
                    # are converted once at the websocket boundary into the
                    # legacy NO-leg scale used by the internal book.
                    "use_yes_price": True,
                },
                {
                    "channels": ["ticker", "trade"],
                    "market_tickers": batch,
                },
            ]
            for params in subscriptions:
                message_id = self._message_id
                self._message_id += 1
                await websocket.send(json.dumps({
                    "id": message_id,
                    "cmd": "subscribe",
                    "params": params,
                }))
            with self._lock:
                self._subscribed.update(batch)

    @staticmethod
    def _ticker(msg):
        return str(msg.get("market_ticker") or msg.get("ticker") or msg.get("market") or "")

    @staticmethod
    def _price_cents(msg, cents_key, dollars_key):
        if msg.get(dollars_key) not in (None, ""):
            return round(_number(msg.get(dollars_key)) * 100.0, 4)
        if msg.get(cents_key) not in (None, ""):
            return round(_number(msg.get(cents_key)), 4)
        return None

    def _check_sequence(self, payload, ticker, message_type):
        if message_type not in {"orderbook_snapshot", "orderbook_delta"}:
            return True
        sid = str(payload.get("sid") or "")
        seq = payload.get("seq")
        if not sid or seq is None:
            return True
        seq = int(seq)
        with self._lock:
            previous = self._sequence_by_sid.get(sid)
            if message_type == "orderbook_snapshot":
                self._sequence_by_sid[sid] = seq
                return True
            if previous is not None and seq != previous + 1:
                self._sequence_gap_count += 1
                self._last_error = f"kalshi_orderbook_sequence_gap sid={sid} expected={previous + 1} received={seq}"
                self._invalidate_books()
                self._reconnect_requested.set()
                return False
            self._sequence_by_sid[sid] = seq
        return True

    def _check_cfbenchmark_sequence(self, payload, index_id):
        sid = str(payload.get("sid") or "")
        seq = payload.get("seq")
        if not sid or seq is None:
            return True
        seq = int(seq)
        with self._lock:
            previous = self._sequence_by_sid.get(sid)
            if previous is not None and seq != previous + 1:
                self._cfbenchmark_sequence_gap_count += 1
                self._last_error = (
                    f"kalshi_cfbenchmark_sequence_gap sid={sid} "
                    f"expected={previous + 1} received={seq}"
                )
                current = dict(self._cfbenchmarks.get(index_id) or {})
                current["sequence_valid"] = False
                current["window_integrity"] = False
                self._cfbenchmarks[index_id] = current
                self._reconnect_requested.set()
                return False
            self._sequence_by_sid[sid] = seq
        return True

    def _handle_cfbenchmark_value(self, payload, msg):
        index_id = str(msg.get("index_id") or "").upper()
        if not index_id or not self._check_cfbenchmark_sequence(payload, index_id):
            return
        try:
            raw = json.loads(msg.get("data") or "{}")
        except (TypeError, json.JSONDecodeError, ValueError):
            raw = {}
        source_ts_ms = int(_number(raw.get("time"), 0.0))
        value = _number(raw.get("value"), 0.0)
        if source_ts_ms <= 0 or value <= 0:
            return
        final_window = msg.get("last_60s_windowed_average_15min") or {}
        window_size = int(_number(final_window.get("window_size"), 0.0))
        window_average = _number(final_window.get("value"), 0.0)
        window_close_ms = (
            ((source_ts_ms // 900000) + 1) * 900000
            if source_ts_ms % 900000
            else source_ts_ms
        )
        expected_window_size = None
        window_time_aligned = True
        source_second_offset_ms = None
        if window_size > 0:
            source_second_offset_ms = source_ts_ms - (window_close_ms - 60000)
            expected_float = source_second_offset_ms / 1000.0
            expected_window_size = int(round(expected_float))
            timestamp_aligned = abs(expected_float - expected_window_size) <= 0.25
            window_time_aligned = bool(
                timestamp_aligned
                and 1 <= expected_window_size <= 60
                and window_size == expected_window_size
            )
        now_unix = time.time()
        received_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            prior = dict(self._cfbenchmarks.get(index_id) or {})
            prior_source_ts = int(_number(prior.get("source_ts_ms"), 0.0))
            if source_ts_ms <= prior_source_ts:
                return
            window_state = dict(self._cfbenchmark_window_state.get(index_id) or {})
            prior_window_close = int(_number(window_state.get("window_close_ts_ms"), 0.0))
            if window_size > 0:
                if prior_window_close != window_close_ms:
                    window_integrity = window_time_aligned
                else:
                    prior_size = int(_number(window_state.get("window_size"), 0.0))
                    window_integrity = bool(
                        window_state.get("window_integrity", True)
                        and window_time_aligned
                    )
                    if prior_size and window_size != prior_size + 1:
                        window_integrity = False
                self._cfbenchmark_window_state[index_id] = {
                    "window_close_ts_ms": window_close_ms,
                    "window_size": window_size,
                    "window_integrity": window_integrity,
                    "window_time_aligned": window_time_aligned,
                    "expected_window_size": expected_window_size,
                }
            else:
                window_integrity = True
            row = {
                "index_id": index_id,
                "value": value,
                "source_ts_ms": source_ts_ms,
                "upstream_received_at_ms": int(_number(msg.get("received_at"), 0.0)),
                "received_at": received_at,
                "received_unix": now_unix,
                "sequence_valid": True,
                "window_integrity": window_integrity,
                "window_time_aligned": window_time_aligned,
                "expected_window_size": expected_window_size,
                "source_second_offset_ms": source_second_offset_ms,
                "window_close_ts_ms": window_close_ms if window_size > 0 else None,
                "window_average": window_average if window_size > 0 and window_average > 0 else None,
                "window_size": window_size,
                "window_start_ts_ms": final_window.get("window_start_ts_ms"),
                "window_end_ts_exclusive": final_window.get("window_end_ts_exclusive"),
            }
            if window_size > 0:
                row["market_snapshots"] = {
                    ticker: dict(self._snapshots.get(ticker) or {})
                    for ticker in self._cfbenchmark_snapshot_tickers
                    if self._snapshots.get(ticker)
                }
            self._cfbenchmarks[index_id] = row
            self._cfbenchmark_history[index_id].append(dict(row))

    def _handle_message(self, raw):
        try:
            payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (TypeError, json.JSONDecodeError, ValueError):
            return
        message_type = str(payload.get("type") or "")
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        ticker = self._ticker(msg)
        if message_type == "error":
            with self._lock:
                self._last_error = f"websocket_error code={msg.get('code')} message={msg.get('msg') or msg}"[:500]
            return
        if message_type == "cfbenchmarks_value":
            self._handle_cfbenchmark_value(payload, msg)
            return
        if not self._check_sequence(payload, ticker, message_type):
            return
        if message_type == "ticker":
            self._update_snapshot(ticker, {
                "best_yes_bid_cents": self._price_cents(msg, "yes_bid", "yes_bid_dollars"),
                "best_yes_entry_price_cents": self._price_cents(msg, "yes_ask", "yes_ask_dollars"),
                "last_trade_price_cents": self._price_cents(msg, "price", "price_dollars"),
                "exchange_ts_ms": msg.get("ts_ms"),
                "yes_bid_size": _number(msg.get("yes_bid_size_fp") if msg.get("yes_bid_size_fp") is not None else msg.get("yes_bid_size"), 0.0),
                "yes_ask_size": _number(msg.get("yes_ask_size_fp") if msg.get("yes_ask_size_fp") is not None else msg.get("yes_ask_size"), 0.0),
                "volume": _number(msg.get("volume_fp") if msg.get("volume_fp") is not None else msg.get("volume"), 0.0),
                "open_interest": _number(msg.get("open_interest_fp") if msg.get("open_interest_fp") is not None else msg.get("open_interest"), 0.0),
                "dollar_volume": _number(msg.get("dollar_volume"), 0.0),
                "dollar_open_interest": _number(msg.get("dollar_open_interest"), 0.0),
            }, message_type)
            return
        if message_type == "trade":
            yes_price = self._price_cents(msg, "yes_price", "yes_price_dollars")
            side = str(msg.get("taker_outcome_side") or msg.get("taker_side") or "").lower()
            direction = 1.0 if side == "yes" else -1.0 if side == "no" else 0.0
            timestamp = _number(
                msg.get("ts_ms")
                if msg.get("ts_ms") is not None
                else msg.get("ts"),
                0.0,
            )
            if timestamp > 1e12:
                timestamp /= 1000.0
            if timestamp <= 0:
                try:
                    timestamp = datetime.fromisoformat(str(msg.get("created_time") or "").replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    timestamp = time.time()
            count = _number(msg.get("count_fp") if msg.get("count_fp") is not None else msg.get("count"), 0.0)
            with self._lock:
                self._trades[ticker].append({
                    "timestamp": timestamp,
                    "price": yes_price,
                    "size": count,
                    "direction": direction,
                })
            flow = self._trade_flow(ticker)
            self._update_snapshot(ticker, {
                "last_trade_price_cents": yes_price,
                "last_trade_side": side,
                "last_trade_ts": timestamp,
                "exchange_ts_ms": msg.get("ts_ms"),
                **flow,
            }, message_type)
            return
        if message_type == "orderbook_snapshot":
            yes_rows = msg.get("yes_dollars_fp") or msg.get("yes_dollars") or msg.get("yes") or []
            no_rows = msg.get("no_dollars_fp") or msg.get("no_dollars") or msg.get("no") or []
            yes_dollars = bool(msg.get("yes_dollars_fp") is not None or msg.get("yes_dollars") is not None)
            no_dollars = bool(msg.get("no_dollars_fp") is not None or msg.get("no_dollars") is not None)
            yes = {round(_number(price) * (100.0 if yes_dollars else 1.0), 4): _number(size) for price, size, *_ in yes_rows if _number(size) > SIZE_EPSILON}
            no_yes_scale = {
                round(_number(price) * (100.0 if no_dollars else 1.0), 4): _number(size)
                for price, size, *_ in no_rows
                if _number(size) > SIZE_EPSILON
            }
            no = {
                round(100.0 - price, 4): size
                for price, size in no_yes_scale.items()
                if 0.0 <= price <= 100.0
            }
            with self._lock:
                self._books[ticker] = {"yes": yes, "no": no}
            self._publish_book(ticker, message_type, exchange_ts_ms=msg.get("ts_ms"))
            return
        if message_type == "orderbook_delta":
            side = str(msg.get("side") or "").lower()
            price = self._price_cents(msg, "price", "price_dollars")
            delta = _number(msg.get("delta_fp") if msg.get("delta_fp") is not None else msg.get("delta"), 0.0)
            if side not in {"yes", "no"} or price is None:
                return
            if side == "no":
                price = round(100.0 - price, 4)
            with self._lock:
                book = self._books.get(ticker)
                if book is None:
                    self._reconnect_requested.set()
                    return
                size = _number(book[side].get(price)) + delta
                if size <= SIZE_EPSILON:
                    book[side].pop(price, None)
                else:
                    book[side][price] = size
            self._publish_book(ticker, message_type, exchange_ts_ms=msg.get("ts_ms"))

    def _trade_flow(self, ticker):
        with self._lock:
            rows = list(self._trades.get(ticker) or ())
        result = {}
        now = time.time()
        for window_seconds in (10, 30, 60, 300):
            cutoff = now - float(window_seconds)
            signed = 0.0
            total = 0.0
            count = 0
            for row in rows:
                if _number(row.get("timestamp")) < cutoff:
                    continue
                notional = max(0.0, _number(row.get("price")) * _number(row.get("size")))
                signed += _number(row.get("direction")) * notional
                total += notional
                count += 1
            result.update({
                f"trade_flow_{window_seconds}s": max(-1.0, min(1.0, signed / total)) if total > 0 else 0.0,
                f"trade_count_{window_seconds}s": count,
                f"trade_notional_{window_seconds}s": round(total, 4),
            })
        return result

    def _publish_book(self, ticker, message_type, exchange_ts_ms=None):
        with self._lock:
            book = self._books.get(ticker) or {"yes": {}, "no": {}}
            yes_levels = sorted(((price, size) for price, size in book["yes"].items() if size > SIZE_EPSILON), reverse=True)
            no_levels = sorted(((price, size) for price, size in book["no"].items() if size > SIZE_EPSILON), reverse=True)
        yes_bid = yes_levels[0][0] if yes_levels else None
        no_bid = no_levels[0][0] if no_levels else None
        yes_ask = 100.0 - no_bid if no_bid is not None else None
        no_ask = 100.0 - yes_bid if yes_bid is not None else None
        if yes_bid is not None and yes_ask is not None and yes_bid > yes_ask + 1e-9:
            reason = (
                f"kalshi_orderbook_crossed ticker={ticker} "
                f"yes_bid={yes_bid:g} yes_ask={yes_ask:g}"
            )
            with self._lock:
                self._book_inconsistency_count += 1
                self._last_error = reason
                current = dict(self._snapshots.get(ticker) or {})
                current.update({
                    "ticker": ticker,
                    "source": "kalshi_websocket",
                    "message_type": message_type,
                    "sequence_valid": False,
                    "book_consistent": False,
                    "book_inconsistency_reason": reason,
                    "received_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "received_unix": time.time(),
                })
                self._snapshots[ticker] = current
                self._reconnect_requested.set()
            return
        top_yes = yes_levels[:5]
        top_no = no_levels[:5]
        yes_quantity = sum(size for _price, size in top_yes)
        no_quantity = sum(size for _price, size in top_no)
        total_quantity = yes_quantity + no_quantity
        book_imbalance = (yes_quantity - no_quantity) / total_quantity if total_quantity > 0 else 0.0
        yes_bid_size = top_yes[0][1] if top_yes else 0.0
        yes_ask_size = top_no[0][1] if top_no else 0.0
        top_total = yes_bid_size + yes_ask_size
        microprice = (
            (yes_ask * yes_bid_size + yes_bid * yes_ask_size) / top_total
            if top_total > 0 and yes_bid is not None and yes_ask is not None
            else None
        )
        midpoint = (
            (yes_bid + yes_ask) / 2.0
            if yes_bid is not None and yes_ask is not None
            else None
        )
        spread = (
            yes_ask - yes_bid
            if yes_bid is not None and yes_ask is not None
            else None
        )
        top_imbalance = (
            (yes_bid_size - yes_ask_size) / top_total
            if top_total > 0
            else 0.0
        )
        depth_by_level = {}
        for count in (1, 3, 5):
            depth_by_level[f"top{count}_yes_depth_contracts"] = round(
                sum(size for _price, size in yes_levels[:count]), 4
            )
            depth_by_level[f"top{count}_no_depth_contracts"] = round(
                sum(size for _price, size in no_levels[:count]), 4
            )
            depth_by_level[f"top{count}_depth_contracts"] = round(
                depth_by_level[f"top{count}_yes_depth_contracts"]
                + depth_by_level[f"top{count}_no_depth_contracts"],
                4,
            )
        outer_yes = yes_levels[min(4, len(yes_levels) - 1)][0] if yes_levels else None
        outer_no = no_levels[min(4, len(no_levels) - 1)][0] if no_levels else None
        outer_ask = 100.0 - outer_no if outer_no is not None else None
        book_slope = (
            (outer_ask - outer_yes) - spread
            if outer_ask is not None and outer_yes is not None and spread is not None
            else None
        )
        inner_depth = depth_by_level["top1_depth_contracts"]
        outer_depth = max(0.0, depth_by_level["top5_depth_contracts"] - inner_depth)
        convexity = (
            (outer_depth - inner_depth) / (outer_depth + inner_depth)
            if outer_depth + inner_depth > 0
            else 0.0
        )
        self._update_snapshot(ticker, {
            "best_yes_bid_cents": yes_bid,
            "best_no_bid_cents": no_bid,
            "best_yes_entry_price_cents": yes_ask,
            "best_no_entry_price_cents": no_ask,
            "yes_depth_contracts": round(yes_quantity, 4),
            "no_depth_contracts": round(no_quantity, 4),
            "book_imbalance": round(book_imbalance, 8),
            "top_imbalance": round(top_imbalance, 8),
            "microprice_yes_cents": round(microprice, 4) if microprice is not None else None,
            "microprice_displacement_yes_cents": round(microprice - midpoint, 4)
            if microprice is not None and midpoint is not None
            else None,
            "spread_yes_cents": round(spread, 4) if spread is not None else None,
            "book_slope_cents": round(book_slope, 4) if book_slope is not None else None,
            "book_convexity": round(max(-1.0, min(1.0, convexity)), 8),
            **depth_by_level,
            "yes_levels": yes_levels,
            "no_levels": no_levels,
            "sequence_valid": True,
            "book_consistent": bool(yes_levels and no_levels),
            "book_inconsistency_reason": "",
            "orderbook_price_convention": "yes_price",
            "exchange_ts_ms": exchange_ts_ms,
            **self._trade_flow(ticker),
        }, message_type)

    def _update_snapshot(self, ticker, values, message_type):
        if not ticker:
            return
        received_unix = time.time()
        now = datetime.fromtimestamp(received_unix, timezone.utc).isoformat(timespec="milliseconds")
        with self._lock:
            current = dict(self._snapshots.get(ticker) or {})
            is_book = message_type in {"orderbook_snapshot", "orderbook_delta"}
            if is_book:
                # None/empty values clear levels and best quotes removed by a delta.
                current.update(values)
                current.update({
                    "book_received_at": now,
                    "book_received_unix": received_unix,
                    "book_revision": int(current.get("book_revision") or 0) + 1,
                    "book_snapshot_valid": True,
                })
            else:
                # Keep ticker quotes for diagnostics without mixing them into depth.
                for key, value in values.items():
                    if message_type == "ticker" and key.startswith(("best_", "yes_bid_size", "yes_ask_size")):
                        current["ticker_" + key] = value
                    elif value is not None:
                        current[key] = value
                current[message_type + "_received_at"] = now
            current.update({
                "ticker": ticker,
                "source": "kalshi_websocket",
                "message_type": message_type,
                "received_at": now,
                "received_unix": received_unix,
            })
            exchange_ts_ms = _number(current.get("exchange_ts_ms"), 0.0)
            if exchange_ts_ms > 0:
                if exchange_ts_ms < 1e12:
                    exchange_ts_ms *= 1000.0
                latency = received_unix * 1000.0 - exchange_ts_ms
                current["event_latency_ms"] = round(max(0.0, latency), 3)
            yes_bid = _number(current.get("best_yes_bid_cents"), 0.0)
            yes_ask = _number(current.get("best_yes_entry_price_cents"), 0.0)
            if yes_bid > 0 and yes_ask > 0 and yes_bid > yes_ask + 1e-9:
                reason = (
                    f"kalshi_quote_crossed ticker={ticker} "
                    f"yes_bid={yes_bid:g} yes_ask={yes_ask:g}"
                )
                current["sequence_valid"] = False
                current["book_consistent"] = False
                current["book_inconsistency_reason"] = reason
                self._book_inconsistency_count += 1
                self._last_error = reason
                self._reconnect_requested.set()
            if is_book and yes_bid > 0 and yes_ask > 0 and current.get("book_consistent", False):
                midpoint = (yes_bid + yes_ask) / 2.0
                history = self._market_history[ticker]
                second = int(received_unix)
                if history and int(history[-1][0]) == second:
                    history[-1] = (float(second), midpoint)
                else:
                    history.append((float(second), midpoint))
                for seconds in (5, 15, 30, 60, 180):
                    target = received_unix - float(seconds)
                    if not history:
                        current[f"yes_mid_change_{seconds}s_pp"] = None
                        continue
                    newer = history[-1]
                    prior = history[0]
                    for observed in reversed(history):
                        if observed[0] <= target:
                            prior = (
                                observed
                                if abs(observed[0] - target) <= abs(newer[0] - target)
                                else newer
                            )
                            break
                        newer = observed
                    current[f"yes_mid_change_{seconds}s_pp"] = (
                        round(midpoint - prior[1], 6)
                        if abs(prior[0] - target) <= max(2.0, seconds * 0.25)
                        else None
                    )
            self._snapshots[ticker] = current
            self._last_message_at = now
