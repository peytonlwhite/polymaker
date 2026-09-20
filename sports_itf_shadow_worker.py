"""Public-GET-only ITF companion. Cannot authenticate or submit/cancel an order."""
import argparse
from collections import Counter
from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

import requests

from runtime_guard import acquire_single_instance, AlreadyRunningError
from sports_itf_shadow import (Experiment, SERIES, append_audit, atomic_json, market_series,
                               now_central, number, pair_markets, parsed, phase, read_json,
                               stamp, underdog)

ROOT = Path(__file__).resolve().parent
BASE = "https://api.elections.kalshi.com/trade-api/v2"
INTERVAL_SECONDS = 60


class PublicKalshi:
    def __init__(self):
        self.session = requests.Session()
        # Do not load .netrc, account credentials, private keys, or live bot settings.
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "Polymaker-ITF-shadow/1", "Cache-Control": "no-cache"})
        self.calls = 0
        self.last_request = 0

    def get(self, path, params=None):
        params = params or {}
        ticker_pattern = r"KXITF(?:WMATCH|MATCH|WDOUBLES|DOUBLES)-[A-Z0-9-]+"
        series_pattern = r"KXITF(?:WMATCH|MATCH|WDOUBLES|DOUBLES)"
        allowed = False
        if path == "/markets":
            allowed = params.get("series_ticker") in SERIES and params.get("status") == "open"
        elif path == "/markets/orderbooks":
            tickers = params.get("tickers")
            allowed = isinstance(tickers, list) and 0 < len(tickers) <= 100 and all(re.fullmatch(ticker_pattern, t) for t in tickers)
        elif re.fullmatch(r"/markets/" + ticker_pattern, path):
            allowed = not params
        elif re.fullmatch(r"/series/" + series_pattern, path):
            allowed = not params
        elif path == "/milestones":
            allowed = params.get("competition") == "ITF"
        elif path == "/live_data/batch":
            ids = params.get("milestone_ids")
            allowed = isinstance(ids, list) and 0 < len(ids) <= 100 and all(re.fullmatch(r"[a-fA-F0-9-]{36}", x) for x in ids)
        if not allowed:
            raise ValueError("ITF public reader rejected a non-research endpoint")
        time.sleep(max(0, .12 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        self.calls += 1
        response = self.session.get(BASE + path, params=params, timeout=(5, 12), allow_redirects=False)
        if response.status_code != 200:
            raise RuntimeError("Public Kalshi HTTP " + str(response.status_code) + " at " + path.split("/")[1])
        return response.json()

    def pages(self, path, key, params):
        rows, cursor, seen = [], None, set()
        for _ in range(30):
            result = self.get(path, {**params, **({"cursor": cursor} if cursor else {})})
            if not isinstance(result.get(key), list):
                raise ValueError("Incomplete Kalshi " + key + " response")
            rows.extend(result[key])
            cursor = result.get("cursor")
            if not cursor:
                return rows
            if cursor in seen:
                raise ValueError("Kalshi pagination repeated a cursor")
            seen.add(cursor)
        raise ValueError("Kalshi pagination incomplete; refusing truncated universe")

    def markets(self):
        return [m for series in SERIES for m in self.pages("/markets", "markets", {
            "series_ticker": series, "status": "open", "limit": 1000})]

    def milestones(self, earliest):
        return self.pages("/milestones", "milestones", {"competition": "ITF", "limit": 500,
                          "minimum_start_date": earliest.astimezone(timezone.utc).isoformat()})

    def books(self, tickers):
        books, times = {}, {}
        for offset in range(0, len(tickers), 100):
            batch = tickers[offset:offset + 100]
            rows = self.get("/markets/orderbooks", {"tickers": batch}).get("orderbooks")
            if not isinstance(rows, list):
                raise ValueError("Missing public orderbooks")
            received = stamp(now_central())
            for row in rows:
                if row.get("ticker") in batch:
                    books[row["ticker"]], times[row["ticker"]] = row, received
            if set(batch) - books.keys():
                raise ValueError("Incomplete public orderbook batch")
        return books, times

    def live(self, ids):
        data = {}
        for offset in range(0, len(ids), 100):
            rows = self.get("/live_data/batch", {"milestone_ids": ids[offset:offset + 100]}).get("live_datas") or []
            for row in rows:
                if row.get("milestone_id") in ids:
                    data[row["milestone_id"]] = row
        return data

    def fees(self):
        output = {}
        for ticker in SERIES:
            row = self.get("/series/" + ticker).get("series") or {}
            multiplier = number(row.get("fee_multiplier"))
            if row.get("ticker") != ticker or row.get("fee_type") != "quadratic" or multiplier is None or multiplier < 0:
                raise ValueError("Unsupported ITF series fee schedule")
            output[ticker] = multiplier
        return output


def milestone_map(rows, pairs):
    mapping = {}
    for row in rows:
        related = set((row.get("related_event_tickers") or []) + (row.get("primary_event_tickers") or []))
        details = row.get("details") or {}
        if details.get("tour") != "ITF":
            continue
        ids = {details.get("first_competitor_id"), details.get("second_competitor_id")}
        for event in related & pairs.keys():
            pair_ids = {m["custom_strike"]["tennis_competitor"] for m in pairs[event]}
            if ids != pair_ids:
                continue
            previous = mapping.get(event)
            if previous is None or (row.get("last_updated_ts") or "") > (previous.get("last_updated_ts") or ""):
                mapping[event] = row
    return mapping


def checked_phase(milestone, live, now):
    if not milestone:
        return "unknown"
    details = (live or {}).get("details") or {}
    observed_ids = {details.get("competitor1_id"), details.get("competitor2_id")} - {None, ""}
    target = milestone.get("details") or {}
    ids = {target.get("first_competitor_id"), target.get("second_competitor_id")}
    if observed_ids and observed_ids != ids:
        return "identity_mismatch"
    return phase(milestone, live, now)


def scan(api, experiment, root, fees):
    started = time.monotonic()
    now = now_central()
    markets = api.markets() if experiment.status(now) == "collecting" else []
    pairs = pair_markets(markets)
    if pairs:
        # Include rescheduled events from the preceding fortnight; never infer start from close_time.
        milestones = milestone_map(api.milestones(now - timedelta(days=15)), pairs)
        ids = sorted({m["id"] for m in milestones.values()})
        initial_live = api.live(ids)
        eligible_pairs = {event: pair for event, pair in pairs.items()
                          if checked_phase(milestones.get(event), initial_live.get(milestones.get(event, {}).get("id")), now_central()) in {"pregame", "live"}}
        tickers = sorted({m["ticker"] for pair in eligible_pairs.values() for m in pair})
        first, first_times = api.books(tickers)
        candidates = {event: pair for event, pair in eligible_pairs.items() if underdog(pair, first)}
        # Confirm the full outcome pair to detect a favorite flip/crossed book.
        confirm_tickers = sorted({m["ticker"] for pair in candidates.values() for m in pair})
        if confirm_tickers:
            time.sleep(1.05)
        second, second_times = api.books(confirm_tickers)
        confirmed_ids = sorted({milestones[e]["id"] for e in candidates})
        final_live = api.live(confirmed_ids)
        status_at = stamp(now_central())
    else:
        milestones, initial_live, candidates, final_live = {}, {}, {}, {}
        first, second, first_times, second_times = {}, {}, {}, {}
        status_at = stamp(now_central())
    entries, frames = [], []
    phases = Counter()
    for event, pair in pairs.items():
        milestone = milestones.get(event, {})
        phases[checked_phase(milestone, initial_live.get(milestone.get("id")), now_central())] += 1
    for event, pair in candidates.items():
        now = now_central()
        milestone = milestones[event]
        live = final_live.get(milestone["id"])
        tickers = [m["ticker"] for m in pair]
        context = {"phase": checked_phase(milestone, live, now), "milestone_id": milestone["id"],
                   "scheduled_start": stamp(parsed(milestone["start_date"])) if parsed(milestone.get("start_date")) else None,
                   "tournament": (milestone.get("details") or {}).get("tournament_name"),
                   "first_at": min(first_times[t] for t in tickers),
                   "confirmed_at": min(second_times[t] for t in tickers), "status_at": status_at,
                   "status_details": (live or {}).get("details") or {}}
        new = experiment.observe(pair, first, second, context, fees[market_series(event)], now)
        entries.extend(new)
        dog = underdog(pair, second)
        frames.append({"event": event, "phase": context["phase"],
                       "selected": dog["selected"]["ticker"] if dog else None,
                       "quotes": [{k: r[k] for k in ("ticker", "side", "bid", "ask", "spread")} for r in dog["routes"]] if dog else [],
                       "entries": [r["id"] for r in new]})
    # Closed markets disappear from open discovery. Query every held contract explicitly.
    by_ticker = {m["ticker"]: m for m in markets}
    settlement_errors = []
    held = sorted({r["ticker"] for r in experiment.state["positions"].values() if r["status"] == "open"})
    for ticker in held:
        if ticker in by_ticker:
            continue
        try:
            result = api.get("/markets/" + ticker).get("market") or {}
            if result.get("ticker") != ticker:
                raise ValueError("Mismatched settlement ticker")
            by_ticker[ticker] = result
        except Exception as exc:
            settlement_errors.append({"ticker": ticker, "error": type(exc).__name__})
    now = now_central()
    settled = experiment.settle(by_ticker, now)
    coverage = {"markets": len(markets), "events": len(pairs), "underdogs": len(candidates),
                "phases": dict(phases), "series_markets": dict(Counter(market_series(m["ticker"]) for m in markets)),
                "new_entries": len(entries), "new_settlements": len(settled),
                "settlement_errors": settlement_errors, "scan_seconds": round(time.monotonic() - started, 2),
                "public_get_calls_total": api.calls, "poll_seconds": INTERVAL_SECONDS}
    experiment.finish_scan(coverage, now)
    # The complete ledger is authoritative; the gzip journal is supplementary evidence.
    experiment.persist()
    append_audit(root, {"at": stamp(now), "scan": experiment.state["scan_count"], "coverage": coverage,
                        "observations": frames, "entries": entries, "settlements": settled}, now)
    return coverage


def implementation_hash(root):
    names = ["sports_itf_shadow.py", "sports_itf_shadow_worker.py"]
    return hashlib.sha256(b"".join((Path(root) / name).read_bytes() for name in names)).hexdigest()


def launch(root=ROOT, once=False, unit=None):
    root = Path(root)
    try:
        acquire_single_instance(root / ".sports_itf_shadow_bot.pid", "ITF shadow research")
    except AlreadyRunningError:
        return 0
    state_path = root / "sports_itf_shadow_state.json"
    report_path = root / "sports_itf_shadow_report.json"
    exists = state_path.exists() or Path(str(state_path) + ".gz").exists()
    if not exists:
        if report_path.exists():
            raise ValueError("ITF report exists without state; refusing to create a duplicate experiment")
        if unit is None:
            portfolio = read_json(root / "sports_paper_portfolio.json")
            unit = (portfolio.get("sports_unit_daily_snapshot") or {}).get("unit_size_at_capture")
    engine = Experiment(state_path, unit_dollars=unit)
    digest = implementation_hash(root)
    if engine.state.get("implementation_hash") not in {None, digest}:
        raise ValueError("ITF research implementation changed; frozen cohort requires review")
    engine.state["implementation_hash"] = digest
    engine.persist()
    api = PublicKalshi()
    fees, fee_checked = {}, 0
    while True:
        started = time.monotonic()
        error = None
        try:
            if engine.status(now_central()) == "clock_error":
                raise ValueError("Clock precedes experiment start")
            if not fees or time.monotonic() - fee_checked >= 3600:
                fees, fee_checked = api.fees(), time.monotonic()
            coverage = scan(api, engine, root, fees)
            if coverage["settlement_errors"]:
                error = "Some exchange settlements could not be fetched; open positions retained"
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)[:240]
            # Reload only the last committed ledger. Never reset it after a failed scan.
            engine = Experiment(state_path)
        report = engine.summary()
        report["worker"] = {"pid": os.getpid(), "error": error, "public_get_only": True,
                            "poll_seconds": INTERVAL_SECONDS, "implementation_hash": digest}
        atomic_json(report_path, report)
        print(json.dumps({"at": report["generated_at"], "status": report["status"],
                          "scan": report["scan_count"], "positions": report["position_count"], "error": error}), flush=True)
        if once or (report["status"] == "complete" and not error):
            return 1 if error else 0
        time.sleep(max(2, INTERVAL_SECONDS - (time.monotonic() - started)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="One public-data shadow scan; never places live orders")
    parser.add_argument("--unit-dollars", type=float, help="Freeze a new experiment's hypothetical 1U budget")
    args = parser.parse_args()
    raise SystemExit(launch(once=args.once, unit=args.unit_dollars))
