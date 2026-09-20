"""Read-only companion for BTC random cycles. Never runs the live scan loop."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlparse

from crypto_btc_random_shadow import RandomCycleLab, btc_window, utcnow, CHICAGO
from crypto_btc_shadow_forecast import forecast, advance_horizons
from crypto_market_regime import atomic_write_json, parse_time
from runtime_guard import acquire_single_instance, AlreadyRunningError

ROOT = Path(__file__).resolve().parent
STATE = "crypto_btc_random_shadow.json"
REPORT = "crypto_btc_random_shadow_report.json"
HEALTH = "crypto_btc_random_shadow_health.json"


def allowed_request(method, url):
    parsed = urlparse(url)
    if method.upper() != "GET" or parsed.scheme != "https":
        return False
    if parsed.hostname == "external-api.kalshi.com":
        return parsed.path == "/trade-api/v2/markets" or parsed.path.startswith(
            ("/trade-api/v2/markets/", "/trade-api/v2/series/"))
    return (parsed.hostname == "api.exchange.coinbase.com"
            and parsed.path == "/products/BTC-USD/candles")


def install_read_only_guard():
    """Process-local defence, including accidental future calls to order APIs."""
    import requests
    original = requests.sessions.Session.request
    def read_only(session, method, url, **kwargs):
        if not allowed_request(method, url):
            raise RuntimeError("BTC shadow worker forbids this HTTP request")
        # Redirects would bypass the original URL check inside requests.
        kwargs["allow_redirects"] = False
        response = original(session, method, url, **kwargs)
        if 300 <= response.status_code < 400:
            raise RuntimeError("BTC shadow worker forbids redirects")
        return response
    requests.sessions.Session.request = read_only


class ProductionQuotes:
    def __init__(self, bot):
        self.bot = bot
        # Public market data needs no credentials or live configuration.
        self.settings = {**bot.DEFAULT_SETTINGS, "CRYPTO_LIVE_ORDER_ENABLED": "false",
                         "CRYPTO_KALSHI_WS_ENABLED": "false",
                         "CRYPTO_LIVE_DEPTH_PREFLIGHT_TIMEOUT_SECONDS": "3"}
        bot.LOG_FILE = ROOT / "crypto_btc_random_adapter.log"

    def markets(self):
        result, seen, cursor = [], set(), None
        for _ in range(10):
            query = {"series_ticker": "KXBTC15M", "status": "open", "limit": 1000}
            if cursor:
                query["cursor"] = cursor
            response = self.bot.http_json(self.bot.KALSHI_BASE+"/markets?"+urlencode(query), timeout=5, retries=0)
            if not isinstance(response.get("markets"), list):
                raise ValueError("Invalid BTC discovery response")
            result.extend(response["markets"])
            cursor = response.get("cursor")
            if not cursor:
                return result
            if cursor in seen:
                raise ValueError("Repeated BTC discovery cursor")
            seen.add(cursor)
        raise ValueError("Incomplete BTC discovery")

    def quote(self, ticker, side, contracts=1):
        started = time.monotonic()
        payload, fetched = self.bot.fetch_fresh_kalshi_orderbook(self.settings, ticker, force_rest=True)
        yes = self.bot.kalshi_orderbook_depth_summary(payload, "yes", required_contracts=1)
        no = self.bot.kalshi_orderbook_depth_summary(payload, "no", required_contracts=1)
        y, n = yes.get("best_entry_price_cents"), no.get("best_entry_price_cents")
        spread = y+n-100 if y is not None and n is not None else None
        depth = self.bot.kalshi_orderbook_depth_summary(payload, side, required_contracts=contracts)
        return {**depth, "ticker": ticker, "side": side, "fetched_at": fetched,
                "sequence_valid": payload.get("sequence_valid", True),
                "book_consistent": spread is not None and spread >= -1e-9
                                   and time.monotonic()-started <= 2,
                "spread_cents": spread, "source": "production_rest_depth"}

    def fee(self):
        return self.bot.kalshi_series_fee_schedule(self.settings, "KXBTC15M")

    def market(self, ticker):
        return self.bot.fetch_low_edge_shadow_market(ticker)

    def candles(self, seconds):
        # Explicit bounds prevent a cached unbounded response from serving
        # several-minute-old data as the most recent candle history.
        end = datetime.fromtimestamp(int(utcnow().timestamp())//seconds*seconds, timezone.utc)
        query = urlencode({"granularity": seconds, "start": (end-timedelta(seconds=seconds*300)).isoformat(),
                           "end": end.isoformat()})
        return self.bot.http_json(
            "https://api.exchange.coinbase.com/products/BTC-USD/candles?"+query,
            timeout=5, retries=0)


def read_context():
    try:
        return json.loads((ROOT/"crypto_market_regime.json").read_text(encoding="utf-8-sig")).get("latest", {})
    except (OSError, ValueError, TypeError):
        return {}


def run(once=False):
    os.chdir(ROOT)
    try:
        acquire_single_instance(ROOT/".crypto_btc_random_bot.pid", "BTC random shadow")
    except AlreadyRunningError:
        return 0
    install_read_only_guard()
    import crypto_paper_bettor as bot
    quotes = ProductionQuotes(bot)
    engine = RandomCycleLab(ROOT/STATE)
    sources = ["crypto_btc_random_shadow.py", "crypto_btc_shadow_forecast.py", "crypto_btc_random_worker.py"]
    implementation = hashlib.sha256(b"".join((ROOT/name).read_bytes() for name in sources)).hexdigest()
    previous = engine.state.get("implementation_hash")
    if previous and previous != implementation:
        raise ValueError("BTC shadow implementation changed; preserve cohort and register a new ledger")
    engine.state["implementation_hash"] = implementation
    engine.persist()
    last_forecast = last_markets = last_settle = -1e9
    markets, prediction = [], {}
    while True:
        started = time.monotonic()
        errors = []
        try:
            now = utcnow()
            if started-last_settle >= 15:
                engine.settle(quotes.market, now)
                last_settle = started
            if started-last_forecast >= 45:
                try:
                    minute, quarter = quotes.candles(60), quotes.candles(900)
                    now = utcnow()
                    # Resolve previous observations first, then calibrate using
                    # only outcomes available at this new forecast timestamp.
                    advance_horizons(engine.state["forecasts"], {}, minute, now)
                    prediction = forecast(minute, quarter, now, engine.state["forecasts"], read_context())
                    advance_horizons(engine.state["forecasts"], prediction, minute, now)
                    engine.state["latest_forecast"] = prediction
                except Exception as exc:
                    prediction = {}
                    engine.state["latest_forecast"] = {"available": False, "status": "source_unavailable",
                                                       "generated_at": utcnow().isoformat()}
                    errors.append("forecast:"+type(exc).__name__)
                last_forecast = started
            engine.prepare(prediction, utcnow())
            if started-last_markets >= 20:
                try:
                    markets = quotes.markets()
                    last_markets = started
                except Exception as exc:
                    markets = []
                    errors.append("discovery:"+type(exc).__name__)
            engine.bind_markets(markets)
            engine.persist()
            for market in markets:
                window = btc_window(market)
                now = utcnow()
                if not window or not window[0] <= now < window[1]:
                    continue
                waiting = [r for r in engine.state["records"]
                           if r["status"] == "waiting" and r.get("ticker") == market["ticker"]]
                if not waiting:
                    continue
                schedule = quotes.fee()
                for side in sorted({r["side"] for r in waiting}):
                    snapshot = quotes.quote(market["ticker"], side)
                    engine.enter(market, snapshot, quotes.quote, schedule)
            engine.state["scan_count"] += 1
            engine.expire(utcnow())
            engine.persist()
        except Exception as exc:
            errors.append("iteration:"+type(exc).__name__)
            # Roll back only in-memory changes. Disk evidence is never reset.
            engine = RandomCycleLab(ROOT/STATE)
        now = utcnow()
        health = {"updated_at": now.astimezone(CHICAGO).isoformat(), "pid": os.getpid(),
                  "status": "degraded" if errors else "running", "errors": errors,
                  "http_policy": "GET public market data only", "live_orders_enabled": False,
                  "market_count": len(markets), "iteration_seconds": round(time.monotonic()-started, 3)}
        report = engine.summary(now)
        report["worker"] = health
        atomic_write_json(ROOT/REPORT, report)
        atomic_write_json(ROOT/HEALTH, health)
        if errors:
            print(json.dumps(health), flush=True)
        if once:
            return 1 if errors else 0
        time.sleep(max(.25, 5-(time.monotonic()-started)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="One read-only shadow iteration")
    raise SystemExit(run(parser.parse_args().once))
