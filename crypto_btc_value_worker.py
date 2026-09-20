"""Independent public-data worker for the registered BTC value experiments."""
import argparse
from datetime import timedelta
import hashlib
import os
from pathlib import Path
import time

from crypto_btc_random_worker import ProductionQuotes, install_read_only_guard
from crypto_btc_value_shadow import ValueLab, read_json, utcnow, local_time, btc_window
from crypto_market_regime import atomic_write_json, parse_time
from runtime_guard import acquire_single_instance, AlreadyRunningError

ROOT = Path(__file__).resolve().parent
PREFIX = "crypto_btc_value_shadow"
SOURCES = ("crypto_btc_value_shadow.py", "crypto_btc_value_worker.py",
           "crypto_btc_random_shadow.py", "crypto_btc_random_worker.py",
           "crypto_execution_safety.py", "crypto_pricing.py", "crypto_evidence.py")


def frozen_implementation(engine):
    digest = hashlib.sha256(b"".join((ROOT/name).read_bytes() for name in SOURCES)).hexdigest()
    if engine.state.get("implementation_hash") not in (None, digest):
        raise ValueError("Frozen implementation changed; preserve this cohort and review before resuming")
    engine.state["implementation_hash"] = digest
    engine.persist()


def current_forecast(now):
    try:
        report = read_json(ROOT/"crypto_btc_random_shadow_report.json")
        generated = parse_time(report.get("generated_at"))
        if report.get("mode") == "shadow_only" and generated and 0 <= (now-generated).total_seconds() <= 90:
            return report.get("forecast", {})
    except (OSError, ValueError, TypeError):
        pass
    return {}


def run(once=False, register=False):
    os.chdir(ROOT)
    try:
        acquire_single_instance(ROOT/".crypto_btc_value_bot.pid", "BTC value shadow")
    except AlreadyRunningError:
        return 0
    install_read_only_guard()
    import crypto_paper_bettor as bot
    quotes = ProductionQuotes(bot)
    bot.LOG_FILE = ROOT/(PREFIX+"_adapter.log")
    source = read_json(ROOT/"crypto_btc_random_shadow.json") if register else None
    engine = ValueLab(ROOT/(PREFIX+".json"), register=register, source=source)
    frozen_implementation(engine)
    last_settle = last_markets = -1e9
    markets = []
    while True:
        started = time.monotonic()
        errors = []
        try:
            now = utcnow()
            if started-last_settle >= 15:
                engine.settle(quotes.market, now)
                last_settle = started
            engine.prepare(current_forecast(utcnow()), utcnow())
            waiting = [r for r in engine.state["records"] if r["status"] == "waiting"]
            if waiting and started-last_markets >= 20:
                markets = quotes.markets()
                last_markets = started
            engine.bind_markets(markets)
            engine.persist()
            for market in markets:
                window = btc_window(market)
                now = utcnow()
                if not window or not window[0] <= now < window[0]+timedelta(seconds=300):
                    continue
                sides = {r["side"] for r in engine.state["records"]
                         if r["status"] == "waiting" and r.get("ticker") == market["ticker"] and now < engine.deadline(r)}
                if sides:
                    schedule = quotes.fee()
                    for side in sorted(sides):
                        snapshot = quotes.quote(market["ticker"], side)
                        engine.enter(market, snapshot, quotes.quote, schedule, utcnow())
            engine.state["scan_count"] += 1
            engine.expire(utcnow())
            engine.persist()
        except Exception as exc:
            errors.append(type(exc).__name__+": "+str(exc)[:200])
            # Recover only the last committed state, never fabricate a fill or reset.
            engine = ValueLab(ROOT/(PREFIX+".json"))
            frozen_implementation(engine)
        report = engine.summary()
        report.update(worker_pid=os.getpid(), errors=errors, status="degraded" if errors else "completed" if report["completed"] else "running")
        atomic_write_json(ROOT/(PREFIX+"_report.json"), report)
        atomic_write_json(ROOT/(PREFIX+"_health.json"), {"generated_at": local_time(utcnow().isoformat()),
            "pid": os.getpid(), "mode": "shadow_only", "status": report["status"], "errors": errors,
            "scan_count": report["scan_count"], "audit": "passed", "live_orders": False})
        if once or report["completed"]:
            return 0
        time.sleep(max(.1, 5-(time.monotonic()-started)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--register", action="store_true", help="Explicitly register the one new cohort if absent")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.once, args.register))
