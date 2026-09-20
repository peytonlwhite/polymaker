"""Background Kalshi WebSocket cache for decision-time sports pricing."""

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
ORDERBOOK_SIZE_EPSILON = 1e-9


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
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
    }


class KalshiSportsStream:
    """Maintain top-of-book and trade snapshots without blocking the scan loop."""

    def __init__(
        self,
        *,
        api_key_id,
        private_key_path="",
        private_key_pem="",
        ws_url=DEFAULT_WS_URL,
        max_tickers=500,
        use_yes_price=True,
    ):
        self.api_key_id = str(api_key_id or "")
        self.private_key_path = str(private_key_path or "")
        self.private_key_pem = str(private_key_pem or "")
        self.ws_url = str(ws_url or DEFAULT_WS_URL)
        self.max_tickers = max(1, int(max_tickers))
        self.use_yes_price = bool(use_yes_price)
        self._lock = threading.RLock()
        self._desired = set()
        self._subscribed = set()
        self._pending_subscriptions = {}
        self._snapshots = {}
        self._books = {}
        self._book_sequences = {}
        self._channel_sequences = {}
        self._quote_history = defaultdict(lambda: deque(maxlen=600))
        self._trade_history = defaultdict(lambda: deque(maxlen=1200))
        self._sequence_gap_count = 0
        self._thread = None
        self._stop = threading.Event()
        self._reconnect_requested = threading.Event()
        self._connected = False
        self._last_error = ""
        self._last_message_at = ""
        self._connection_count = 0
        self._message_id = 1

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="kalshi-sports-stream",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def subscribe(self, tickers):
        cleaned = [str(ticker or "").strip() for ticker in tickers if str(ticker or "").strip()]
        with self._lock:
            for ticker in cleaned:
                if len(self._desired) >= self.max_tickers and ticker not in self._desired:
                    break
                self._desired.add(ticker)

    def replace_subscriptions(self, tickers):
        cleaned = []
        seen = set()
        for ticker in tickers:
            ticker = str(ticker or "").strip()
            if not ticker or ticker in seen:
                continue
            cleaned.append(ticker)
            seen.add(ticker)
            if len(cleaned) >= self.max_tickers:
                break
        replacement = set(cleaned)
        with self._lock:
            changed = replacement != self._desired
            self._desired = replacement
            # Snapshots and histories for tickers that are no longer managed by
            # this stream must not remain eligible for quote overlays. Besides
            # bounding long-running memory use, this prevents a recently
            # removed ticker from looking stream-backed while its live value is
            # actually coming from an obsolete subscription.
            obsolete = set(self._snapshots) - replacement
            for ticker in obsolete:
                self._snapshots.pop(ticker, None)
                self._books.pop(ticker, None)
                self._book_sequences.pop(ticker, None)
                self._quote_history.pop(ticker, None)
                self._trade_history.pop(ticker, None)
            connected = self._connected
        if changed and connected:
            # A clean reconnect drops obsolete market subscriptions and gets a
            # fresh orderbook snapshot for the current scan's highest-priority set.
            self._reconnect_requested.set()

    def snapshot(self, ticker, max_age_seconds=15.0):
        with self._lock:
            row = dict(self._snapshots.get(str(ticker or "")) or {})
        if not row:
            return {}
        received = row.get("quote_received_unix")
        age = max(0.0, time.time() - float(received or 0))
        row["age_seconds"] = round(age, 3)
        row["fresh"] = received is not None and age <= float(max_age_seconds)
        row.pop("received_unix", None)
        row.pop("quote_received_unix", None)
        return row

    def wait_for_fresh(self, tickers, *, max_age_seconds=15.0, timeout_seconds=1.5):
        requested = []
        seen = set()
        for ticker in tickers:
            ticker = str(ticker or "").strip()
            if ticker and ticker not in seen:
                requested.append(ticker)
                seen.add(ticker)
        deadline = time.time() + max(0.0, float(timeout_seconds or 0))
        fresh = []
        while requested:
            fresh = [
                ticker
                for ticker in requested
                if self.snapshot(ticker, max_age_seconds=max_age_seconds).get("fresh")
            ]
            if len(fresh) >= len(requested) or time.time() >= deadline:
                break
            time.sleep(0.05)
        return {
            "requested_tickers": len(requested),
            "fresh_tickers": len(fresh),
            "missing_or_stale_tickers": max(0, len(requested) - len(fresh)),
        }

    def status(self):
        with self._lock:
            return {
                "enabled": True,
                "connected": self._connected,
                "desired_tickers": len(self._desired),
                "subscribed_tickers": len(self._subscribed),
                "pending_subscription_batches": len(self._pending_subscriptions),
                "snapshot_count": len(self._snapshots),
                "last_message_at": self._last_message_at,
                "last_error": self._last_error,
                "connection_count": self._connection_count,
                "sequence_gap_count": self._sequence_gap_count,
                "orderbook_yes_price_scale": self.use_yes_price,
                "thread_alive": bool(self._thread and self._thread.is_alive()),
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
                    self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                await asyncio.sleep(2.0)

    async def _connect_once(self):
        import websockets

        if not self.api_key_id:
            raise RuntimeError("kalshi_websocket_api_key_missing")
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
            max_queue=2048,
        ) as websocket:
            with self._lock:
                self._connected = True
                self._subscribed.clear()
                self._pending_subscriptions.clear()
                self._books.clear()
                self._book_sequences.clear()
                self._channel_sequences.clear()
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
            pending_tickers = {
                ticker
                for batch in self._pending_subscriptions.values()
                for ticker in batch
            }
            pending = sorted(self._desired - self._subscribed - pending_tickers)
        for offset in range(0, len(pending), 100):
            batch = pending[offset : offset + 100]
            if not batch:
                continue
            message_id = self._message_id
            self._message_id += 1
            await websocket.send(
                json.dumps(
                    {
                        "id": message_id,
                        "cmd": "subscribe",
                        "params": {
                            "channels": ["ticker", "trade", "orderbook_delta"],
                            "market_tickers": batch,
                            # Pin Kalshi's unified yes-leg scale now instead of
                            # depending on the server's changing default.
                            "use_yes_price": self.use_yes_price,
                        },
                    }
                )
            )
            with self._lock:
                # A send is not a subscription.  Promote this batch only after
                # the server acknowledges it so rejected requests can retry.
                self._pending_subscriptions[message_id] = set(batch)

    @staticmethod
    def _ticker(msg):
        return str(
            msg.get("market_ticker")
            or msg.get("ticker")
            or msg.get("market")
            or ""
        )

    @staticmethod
    def _cents(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        # WebSocket messages may use dollars for *_dollars fields, but the
        # standard ticker/orderbook channels use integer cents.
        return round(number, 4)

    @staticmethod
    def _dollars_to_cents(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return round(number * 100.0, 4)

    @staticmethod
    def _quantity(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _message_price(cls, msg, cents_key, dollars_key):
        if msg.get(dollars_key) is not None:
            return cls._dollars_to_cents(msg.get(dollars_key))
        return cls._cents(msg.get(cents_key))

    def _update_snapshot(self, ticker, values, message_type):
        if not ticker:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        now_unix = time.time()
        with self._lock:
            current = dict(self._snapshots.get(ticker) or {})
            old_bid = self._cents(current.get("yes_bid"))
            old_ask = self._cents(current.get("yes_ask"))
            book_update = message_type in {"orderbook_snapshot", "orderbook_delta"}
            # An authoritative empty book side removes the prior executable
            # quote and size. Ignoring its None values resurrects lost liquidity.
            current.update({
                key: value for key, value in values.items()
                if value is not None or (book_update and key in {
                    "yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size",
                })
            })
            current.update(
                {
                    "ticker": ticker,
                    "source": "kalshi_websocket",
                    "message_type": message_type,
                    "received_at": now,
                    "received_unix": now_unix,
                }
            )
            if book_update or any(values.get(key) is not None for key in ("yes_bid", "yes_ask")):
                current["quote_received_at"] = now
                current["quote_received_unix"] = current["received_unix"]
            yes_bid = self._cents(current.get("yes_bid"))
            yes_ask = self._cents(current.get("yes_ask"))
            if "yes_bid" in current:
                current["no_ask"] = round(100.0 - yes_bid, 4) if yes_bid is not None else None
            if "yes_ask" in current:
                current["no_bid"] = round(100.0 - yes_ask, 4) if yes_ask is not None else None
            if (
                yes_bid is not None
                and yes_ask is not None
                and (old_bid != yes_bid or old_ask != yes_ask)
            ):
                self._quote_history[ticker].append(
                    {
                        "at": now_unix,
                        "bid": yes_bid,
                        "ask": yes_ask,
                        "mid": (yes_bid + yes_ask) / 2.0,
                    }
                )
            quote_history = self._quote_history[ticker]
            while quote_history and now_unix - quote_history[0]["at"] > 600.0:
                quote_history.popleft()
            if quote_history:
                current["quote_changes_60s"] = sum(
                    1 for row in quote_history if now_unix - row["at"] <= 60.0
                )
                latest_mid = quote_history[-1]["mid"]
                for seconds, field in (
                    (30.0, "mid_velocity_30s_cents"),
                    (120.0, "mid_velocity_120s_cents"),
                    (300.0, "mid_velocity_300s_cents"),
                ):
                    baseline = min(
                        quote_history,
                        key=lambda row: abs(row["at"] - (now_unix - seconds)),
                    )
                    current[field] = round(latest_mid - baseline["mid"], 4)
            exchange_ts_ms = self._quantity(current.get("exchange_ts_ms"))
            if exchange_ts_ms is not None:
                if exchange_ts_ms < 10_000_000_000:
                    exchange_ts_ms *= 1000.0
                current["exchange_latency_ms"] = round(
                    max(0.0, now_unix * 1000.0 - exchange_ts_ms),
                    3,
                )
            trade_history = self._trade_history[ticker]
            while trade_history and now_unix - trade_history[0]["at"] > 600.0:
                trade_history.popleft()
            recent_trades = [row for row in trade_history if now_unix - row["at"] <= 60.0]
            current["recent_trade_count_60s"] = len(recent_trades)
            current["recent_trade_contracts_60s"] = round(
                sum(float(row.get("count") or 0) for row in recent_trades), 3
            )
            current["recent_yes_taker_contracts_60s"] = round(
                sum(
                    float(row.get("count") or 0)
                    for row in recent_trades
                    if row.get("taker_outcome_side") == "yes"
                ),
                3,
            )
            current["recent_no_taker_contracts_60s"] = round(
                sum(
                    float(row.get("count") or 0)
                    for row in recent_trades
                    if row.get("taker_outcome_side") == "no"
                ),
                3,
            )
            if trade_history:
                current["last_trade_age_seconds"] = round(
                    max(0.0, now_unix - trade_history[-1]["at"]), 3
                )
            self._snapshots[ticker] = current
            self._last_message_at = now

    def _record_trade(self, ticker, msg):
        if not ticker:
            return
        count = self._quantity(
            msg.get("count_fp") if msg.get("count_fp") is not None else msg.get("count")
        )
        trade_ts = self._quantity(msg.get("ts_ms"))
        if trade_ts is not None:
            if trade_ts > 10_000_000_000:
                trade_ts /= 1000.0
        else:
            trade_ts = self._quantity(msg.get("ts")) or time.time()
        with self._lock:
            self._trade_history[ticker].append(
                {
                    "at": float(trade_ts),
                    "count": max(0.0, float(count or 0.0)),
                    "taker_outcome_side": str(
                        msg.get("taker_outcome_side")
                        or msg.get("taker_side")
                        or ""
                    ).lower(),
                    "taker_book_side": str(msg.get("taker_book_side") or "").lower(),
                    "is_block_trade": bool(msg.get("is_block_trade")),
                    "trade_id": msg.get("trade_id"),
                }
            )

    def _handle_message(self, raw):
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return
        message_type = str(payload.get("type") or "")
        msg = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        ticker = self._ticker(msg)
        message_id = payload.get("id") if payload.get("id") is not None else msg.get("id")

        if message_type == "subscribed":
            with self._lock:
                batch = self._pending_subscriptions.pop(message_id, set())
                self._subscribed.update(batch)
            return

        if message_type == "error":
            with self._lock:
                batch = self._pending_subscriptions.pop(message_id, set())
                if int(msg.get("code") or 0) == 6:
                    # "Already subscribed" is an idempotent success.
                    self._subscribed.update(batch)
                elif batch:
                    self._reconnect_requested.set()
                self._last_error = (
                    f"websocket_error code={msg.get('code')} message={msg.get('msg') or msg}"
                )[:500]
            return
        if message_type == "ticker":
            self._update_snapshot(
                ticker,
                {
                    "yes_bid": self._message_price(msg, "yes_bid", "yes_bid_dollars"),
                    "yes_ask": self._message_price(msg, "yes_ask", "yes_ask_dollars"),
                    "last_price": self._message_price(msg, "price", "price_dollars"),
                    "volume": self._quantity(
                        msg.get("volume_fp")
                        if msg.get("volume_fp") is not None
                        else msg.get("volume")
                    ),
                    "open_interest": self._quantity(
                        msg.get("open_interest_fp")
                        if msg.get("open_interest_fp") is not None
                        else msg.get("open_interest")
                    ),
                    "yes_bid_size": self._quantity(msg.get("yes_bid_size_fp")),
                    "yes_ask_size": self._quantity(msg.get("yes_ask_size_fp")),
                    "last_trade_size": self._quantity(msg.get("last_trade_size_fp")),
                    "dollar_volume": self._quantity(msg.get("dollar_volume")),
                    "dollar_open_interest": self._quantity(msg.get("dollar_open_interest")),
                    "exchange_ts_ms": msg.get("ts_ms"),
                    "exchange_time": msg.get("time"),
                },
                message_type,
            )
            return
        if message_type == "trade":
            self._record_trade(ticker, msg)
            self._update_snapshot(
                ticker,
                {
                    "last_trade_price": (
                        self._dollars_to_cents(msg.get("yes_price_dollars"))
                        if msg.get("yes_price_dollars") is not None
                        else self._message_price(msg, "yes_price", "price_dollars")
                    ),
                    "last_trade_count": self._quantity(
                        msg.get("count_fp")
                        if msg.get("count_fp") is not None
                        else msg.get("count")
                    ),
                    "last_trade_size": self._quantity(
                        msg.get("count_fp")
                        if msg.get("count_fp") is not None
                        else msg.get("count")
                    ),
                    "last_trade_no_price": (
                        self._dollars_to_cents(msg.get("no_price_dollars"))
                        if msg.get("no_price_dollars") is not None
                        else self._cents(msg.get("no_price"))
                    ),
                    "last_trade_taker_outcome_side": (
                        msg.get("taker_outcome_side") or msg.get("taker_side")
                    ),
                    "last_trade_taker_book_side": msg.get("taker_book_side"),
                    "last_trade_is_block": bool(msg.get("is_block_trade")),
                    "last_trade_id": msg.get("trade_id"),
                    "last_trade_ts": msg.get("ts_ms") or msg.get("ts") or msg.get("created_time"),
                    "exchange_ts_ms": msg.get("ts_ms"),
                },
                message_type,
            )
            return
        if message_type == "orderbook_snapshot":
            yes_dollars = msg.get("yes_dollars_fp")
            no_dollars = msg.get("no_dollars_fp")
            yes = {
                float(self._dollars_to_cents(price) if yes_dollars is not None else price): float(size)
                for price, size in (yes_dollars if yes_dollars is not None else (msg.get("yes") or []))
                if float(size) > ORDERBOOK_SIZE_EPSILON
            }
            no = {
                float(self._dollars_to_cents(price) if no_dollars is not None else price): float(size)
                for price, size in (no_dollars if no_dollars is not None else (msg.get("no") or []))
                if float(size) > ORDERBOOK_SIZE_EPSILON
            }
            with self._lock:
                self._books[ticker] = {"yes": yes, "no": no}
                sequence = payload.get("seq") if payload.get("seq") is not None else msg.get("seq")
                self._book_sequences[ticker] = int(sequence) if sequence is not None else None
                sid = payload.get("sid") if payload.get("sid") is not None else msg.get("sid")
                if sid is not None and sequence is not None:
                    self._channel_sequences[str(sid)] = int(sequence)
            self._publish_book(ticker, message_type)
            return
        if message_type == "orderbook_delta":
            side = str(msg.get("side") or "").lower()
            price = (
                self._dollars_to_cents(msg.get("price_dollars"))
                if msg.get("price_dollars") is not None
                else self._cents(msg.get("price"))
            )
            delta = self._quantity(
                msg.get("delta_fp")
                if msg.get("delta_fp") is not None
                else msg.get("delta")
            )
            if side not in {"yes", "no"} or price is None or delta is None:
                return
            with self._lock:
                sequence = payload.get("seq") if payload.get("seq") is not None else msg.get("seq")
                sequence = int(sequence) if sequence is not None else None
                sid = payload.get("sid") if payload.get("sid") is not None else msg.get("sid")
                sequence_key = f"sid:{sid}" if sid is not None else f"ticker:{ticker}"
                previous = (
                    self._channel_sequences.get(str(sid))
                    if sid is not None
                    else self._book_sequences.get(ticker)
                )
                if ticker not in self._books:
                    self._invalidate_orderbook(ticker, "orderbook_delta_before_snapshot")
                    return
                if sequence is not None and previous is not None:
                    if sequence <= previous:
                        return
                    if sequence != previous + 1:
                        self._invalidate_orderbooks(
                            f"orderbook_sequence_gap key={sequence_key} expected={previous + 1} received={sequence}",
                        )
                        return
                book = self._books[ticker]
                new_size = float(book[side].get(price, 0.0)) + delta
                if new_size <= ORDERBOOK_SIZE_EPSILON:
                    book[side].pop(price, None)
                else:
                    book[side][price] = new_size
                if sequence is not None:
                    self._book_sequences[ticker] = sequence
                    if sid is not None:
                        self._channel_sequences[str(sid)] = sequence
            self._publish_book(ticker, message_type, exchange_ts_ms=msg.get("ts_ms"))

    def _invalidate_orderbook(self, ticker, reason):
        """Drop corrupt depth and force a snapshot-producing reconnect."""
        self._books.pop(ticker, None)
        self._book_sequences.pop(ticker, None)
        current = dict(self._snapshots.get(ticker) or {})
        current.pop("yes_bid_size", None)
        current.pop("yes_ask_size", None)
        current.pop("yes_bid_levels", None)
        current.pop("yes_ask_levels", None)
        current.pop("no_bid_levels", None)
        current.pop("no_ask_levels", None)
        current["orderbook_valid"] = False
        current["orderbook_error"] = reason
        self._snapshots[ticker] = current
        self._sequence_gap_count += 1
        self._last_error = reason[:500]
        self._reconnect_requested.set()

    def _invalidate_orderbooks(self, reason):
        """Invalidate every book after a true subscription-channel gap."""
        tickers = set(self._books) | set(self._snapshots)
        self._books.clear()
        self._book_sequences.clear()
        self._channel_sequences.clear()
        for ticker in tickers:
            current = dict(self._snapshots.get(ticker) or {})
            current.pop("yes_bid_size", None)
            current.pop("yes_ask_size", None)
            current.pop("yes_bid_levels", None)
            current.pop("yes_ask_levels", None)
            current.pop("no_bid_levels", None)
            current.pop("no_ask_levels", None)
            current["orderbook_valid"] = False
            current["orderbook_error"] = reason
            self._snapshots[ticker] = current
        self._sequence_gap_count += 1
        self._last_error = reason[:500]
        self._reconnect_requested.set()

    def _publish_book(self, ticker, message_type, exchange_ts_ms=None):
        with self._lock:
            book = self._books.get(ticker) or {"yes": {}, "no": {}}
            for side in ("yes", "no"):
                negligible = [
                    price
                    for price, size in book[side].items()
                    if float(size) <= ORDERBOOK_SIZE_EPSILON
                ]
                for price in negligible:
                    book[side].pop(price, None)
            yes_bid = max(book["yes"], default=None)
            no_book_top = (
                min(book["no"], default=None)
                if self.use_yes_price
                else max(book["no"], default=None)
            )
            yes_bid_size = book["yes"].get(yes_bid) if yes_bid is not None else None
            yes_ask_size = book["no"].get(no_book_top) if no_book_top is not None else None
            sequence = self._book_sequences.get(ticker)
            yes_bid_levels = [
                {"price": round(float(price), 4), "size": round(float(size), 4)}
                for price, size in sorted(book["yes"].items(), reverse=True)[:10]
            ]
            yes_ask_levels = [
                {
                    "price": round(
                        float(price) if self.use_yes_price else 100.0 - float(price),
                        4,
                    ),
                    "size": round(float(size), 4),
                }
                for price, size in sorted(
                    book["no"].items(),
                    reverse=not self.use_yes_price,
                )[:10]
            ]
            yes_ask_levels.sort(key=lambda row: row["price"])
            no_bid_levels = [
                {
                    "price": round(
                        100.0 - float(price) if self.use_yes_price else float(price),
                        4,
                    ),
                    "size": round(float(size), 4),
                }
                for price, size in sorted(
                    book["no"].items(),
                    reverse=not self.use_yes_price,
                )[:10]
            ]
            no_bid_levels.sort(key=lambda row: row["price"], reverse=True)
            no_ask_levels = [
                {"price": round(100.0 - float(price), 4), "size": round(float(size), 4)}
                for price, size in sorted(book["yes"].items(), reverse=True)[:10]
            ]
            no_ask_levels.sort(key=lambda row: row["price"])
        yes_ask = (
            no_book_top
            if self.use_yes_price
            else 100.0 - no_book_top
        ) if no_book_top is not None else None
        self._update_snapshot(
            ticker,
            {
                "yes_bid": yes_bid,
                "yes_ask": yes_ask,
                "yes_bid_size": yes_bid_size,
                "yes_ask_size": yes_ask_size,
                "yes_bid_levels": yes_bid_levels,
                "yes_ask_levels": yes_ask_levels,
                "no_bid_levels": no_bid_levels,
                "no_ask_levels": no_ask_levels,
                "orderbook_sequence": sequence,
                "orderbook_valid": True,
                "orderbook_yes_price_scale": self.use_yes_price,
                "orderbook_exchange_ts_ms": exchange_ts_ms,
                "exchange_ts_ms": exchange_ts_ms,
            },
            message_type,
        )
