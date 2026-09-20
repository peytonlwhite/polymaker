"""Public-data worker for three prospective ITF price-quality simulations."""
import argparse
from collections import Counter
from datetime import timedelta
import gzip
import hashlib
import json
import os
from pathlib import Path
import time

from runtime_guard import acquire_single_instance, AlreadyRunningError
from sports_itf_price_quality import PriceQuality, STATE, REPORT
from sports_itf_shadow import Experiment, atomic_json, market_series, now_central, pair_markets, parsed, stamp, underdog
from sports_itf_shadow_worker import PublicKalshi, checked_phase, milestone_map

ROOT = Path(__file__).resolve().parent
INTERVAL = 60


def implementation_hash(root):
    names = ("sports_itf_price_quality.py", "sports_itf_price_quality_worker.py", "sports_itf_shadow.py",
             "sports_itf_shadow_worker.py", "sports_itf_followups.py", "itf_shadow_accounting.py")
    return hashlib.sha256(b"".join((Path(root) / name).read_bytes() for name in names)).hexdigest()


def scan(api, engine, root, fees):
    began = time.monotonic()
    markets = api.markets() if engine.status(now_central()) == "collecting" else []
    all_pairs = pair_markets(markets)
    excluded = set(engine.state["excluded_events"])
    pairs = {event: pair for event, pair in all_pairs.items() if event not in excluded}
    candidates, phases, entries = {}, Counter(), []
    if pairs:
        milestones = milestone_map(api.milestones(now_central() - timedelta(days=15)), pairs)
        live = api.live(sorted({m["id"] for m in milestones.values()}))
        eligible = {}
        for event, pair in sorted(pairs.items()):
            milestone = milestones.get(event, {})
            match_phase = checked_phase(milestone, live.get(milestone.get("id")), now_central())
            phases[match_phase] += 1
            if match_phase in {"pregame", "live"}:
                eligible[event] = pair
        first, first_times = api.books(sorted({m["ticker"] for pair in eligible.values() for m in pair}))
        candidates = {e: pair for e, pair in eligible.items() if underdog(pair, first)}
        tickers = sorted({m["ticker"] for pair in candidates.values() for m in pair})
        if tickers:
            time.sleep(1.05)
        second, second_times = api.books(tickers)
        final_live = api.live(sorted({milestones[e]["id"] for e in candidates}))
        status_at = stamp(now_central())
        for event, pair in candidates.items():
            now = now_central()
            milestone = milestones[event]
            status = final_live.get(milestone["id"])
            context = {"phase": checked_phase(milestone, status, now), "milestone_id": milestone["id"],
                       "scheduled_start": stamp(parsed(milestone["start_date"])) if parsed(milestone.get("start_date")) else None,
                       "tournament": (milestone.get("details") or {}).get("tournament_name"),
                       "first_at": min(first_times[m["ticker"]] for m in pair),
                       "confirmed_at": min(second_times[m["ticker"]] for m in pair), "status_at": status_at,
                       "status_details": (status or {}).get("details") or {}}
            entries.extend(engine.observe(pair, first, second, context, fees[market_series(event)], now))
    by_ticker = {m["ticker"]: m for m in markets}
    errors = []
    for ticker in sorted({r["ticker"] for r in engine.state["positions"].values() if r["status"] == "open"}):
        if ticker in by_ticker:
            continue
        try:
            market = api.get("/markets/" + ticker).get("market") or {}
            if market.get("ticker") != ticker:
                raise ValueError("Mismatched settlement ticker")
            by_ticker[ticker] = market
        except Exception as exc:
            errors.append({"ticker": ticker, "error": type(exc).__name__})
    now = now_central()
    settled = engine.settle(by_ticker, now)
    coverage = {"markets": len(markets), "events": len(all_pairs), "new_events": len(pairs),
                "candidates": len(candidates), "phases": dict(phases), "new_entries": len(entries),
                "new_settlements": len(settled), "settlement_errors": errors,
                "scan_seconds": round(time.monotonic() - began, 2), "public_get_calls_total": api.calls}
    engine.finish_scan(coverage, now)
    engine.persist()
    archive = Path(root) / "archives" / "sports_itf_price_quality" / (now.strftime("%Y-%m-%d") + ".jsonl.gz")
    archive.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(archive, "at", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": stamp(now), "coverage": coverage, "entries": entries, "settlements": settled}, allow_nan=False) + "\n")
    return coverage


def launch(root=ROOT, *, register=False, once=False):
    root = Path(root)
    try:
        acquire_single_instance(root / ".sports_itf_price_quality_bot.pid", "ITF price-quality shadow tests")
    except AlreadyRunningError:
        return 0
    source = Experiment(root / "sports_itf_shadow_state.json").state if register else None
    engine = PriceQuality(root, register=register, source=source)
    digest = implementation_hash(root)
    if engine.state.get("implementation_hash") not in {None, digest}:
        raise ValueError("Price-quality implementation changed; preserve frozen cohort")
    engine.state["implementation_hash"] = digest
    engine.persist()
    api, fees, checked = PublicKalshi(), {}, 0
    while True:
        began, error = time.monotonic(), None
        try:
            if engine.status(now_central()) == "clock_error":
                raise ValueError("Clock precedes registration")
            if engine.status(now_central()) == "collecting" and (not fees or time.monotonic() - checked >= 3600):
                fees, checked = api.fees(), time.monotonic()
            coverage = scan(api, engine, root, fees)
            if coverage["settlement_errors"]:
                error = "Some settlements unavailable; open positions retained"
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)[:240]
            engine = PriceQuality(root)
        report = engine.summary()
        report["worker"] = {"pid": os.getpid(), "error": error, "public_get_only": True,
                            "poll_seconds": INTERVAL, "implementation_hash": digest}
        atomic_json(root / REPORT, report)
        print(json.dumps({"at": report["generated_at"], "status": report["status"], "scan": report["scan_count"],
                          "positions": report["position_count"], "error": error}), flush=True)
        if once or (report["status"] == "complete" and not error):
            return 1 if error else 0
        time.sleep(max(2, INTERVAL - (time.monotonic() - began)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", action="store_true", help="Register once; resume normally without this flag")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    raise SystemExit(launch(register=args.register, once=args.once))
