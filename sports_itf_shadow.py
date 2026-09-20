"""Prospective ITF research. Pure simulation: no account, settings, or executor imports."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
VERSION = "itf-upsets-v1"
SERIES = {
    "KXITFMATCH": "Men singles", "KXITFWMATCH": "Women singles",
    "KXITFDOUBLES": "Men doubles", "KXITFWDOUBLES": "Women doubles",
}
STRATEGIES = {
    "all_underdogs": {"name": "Every underdog", "min_price": .0001, "max_price": .4999,
                      "description": "1U on every identifiable, executable ITF underdog; pregame or live."},
    "mid_price": {"name": "Mid-price dogs", "min_price": .20, "max_price": .40, "max_spread": .08,
                  "description": "1U at 20–40 cents, with a bid/ask spread of at most 8 cents."},
    "longshots": {"name": "Longshots", "min_price": .05, "max_price": .1999, "max_spread": .08,
                  "description": "1U at 5–under 20 cents, with a bid/ask spread of at most 8 cents."},
    "strengthening": {"name": "Strengthening dogs", "min_price": .10, "max_price": .45,
                      "max_spread": .08, "momentum": True,
                      "description": "1U at 10–45 cents after the executable bid rises at least 3 cents over 5–30 minutes, confirmed twice; spread at most 8 cents."},
}
RULES = {"version": VERSION, "series": SERIES, "strategies": STRATEGIES,
         "days": 7, "units_per_entry": 1, "budget_includes_fees": True,
         "one_entry_per_event_per_strategy": True, "confirmation_min_seconds": 1,
         "first_book_max_age_seconds": 30, "confirmed_book_max_age_seconds": 15,
         "fee_model": "quadratic taker; ceil to cents per price level, conservative",
         "simulation": "minimum displayed depth at identical prices in two public snapshots; hold to exchange settlement"}
RULES_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


def now_central():
    return datetime.now(CENTRAL)


def parsed(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else None
    except (TypeError, ValueError):
        return None


def stamp(now):
    return now.astimezone(CENTRAL).isoformat(timespec="milliseconds")


def number(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def read_json(path):
    """Missing/corrupt state raises; an archived snapshot is readable without resetting it."""
    path = Path(path)
    if not path.exists() and Path(str(path) + ".gz").exists():
        path = Path(str(path) + ".gz")
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig") as handle:
        return json.load(handle)


def atomic_json(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_audit(root, frame, now):
    path = Path(root) / "archives" / "sports_itf_shadow" / (now.astimezone(CENTRAL).date().isoformat() + ".jsonl.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "at", encoding="utf-8") as handle:
        handle.write(json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def iter_audit(path):
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def market_series(ticker):
    return str(ticker).split("-", 1)[0]


def pair_markets(markets):
    """Use exact event/competitor IDs. Never infer an opponent from no_sub_title."""
    groups = defaultdict(dict)
    for row in markets:
        ticker, event = row.get("ticker", ""), row.get("event_ticker", "")
        if (market_series(ticker) in SERIES and market_series(event) == market_series(ticker)
                and ticker.startswith(event + "-") and row.get("market_type") == "binary"
                and row.get("status") in {"active", "open"} and not row.get("result")
                and number(row.get("notional_value_dollars")) == 1):
            groups[event][ticker] = row
    pairs = {}
    for event, group in groups.items():
        rows = sorted(group.values(), key=lambda row: row["ticker"])
        if len(rows) != 2:
            continue
        ids = [(r.get("custom_strike") or {}).get("tennis_competitor") for r in rows]
        names = [str(r.get("yes_sub_title") or "").strip() for r in rows]
        if not all(ids) or ids[0] == ids[1] or not all(names) or names[0].casefold() == names[1].casefold():
            continue
        if not all(str(r.get("rules_primary") or "").startswith("If " + name + " wins ")
                   and "professional tennis match" in str(r.get("rules_primary")) for r, name in zip(rows, names)):
            continue
        pairs[event] = rows
    return pairs


def phase(milestone, live, now):
    """Exchange open/close/expiration times are NOT match start times."""
    details = (live or {}).get("details") or {}
    status = str(details.get("status") or "").lower()
    widget = str(details.get("widget_status") or "").lower()
    start = parsed((milestone or {}).get("start_date"))
    end = parsed((milestone or {}).get("end_date"))
    if (details.get("winner") or (end and end <= now)
            or status in {"closed", "ended", "finished", "retired", "cancelled", "canceled", "walkover"}
            or widget in {"finished", "cancelled", "canceled", "walkover", "retired"}):
        return "finished"
    if status in {"not_started", "scheduled"}:
        return "pregame"
    if status in {"in_progress", "inprogress", "live", "started", "playing"}:
        return "live"
    # A future, explicit scheduled milestone is safe if no score feed exists yet.
    if not details and start and start > now and (milestone.get("details") or {}).get("status") == "SCH":
        return "pregame"
    return "unknown"


def levels(book, side):
    fp = (book or {}).get("orderbook_fp")
    if fp is not None:
        raw, divisor = fp.get(side + "_dollars") or [], 1
    else:
        raw, divisor = ((book or {}).get("orderbook") or {}).get(side) or [], 100
    out = {}
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price, count = number(row[0]), number(row[1])
        if price is not None and count is not None and 0 < price / divisor < 1 and count > 0:
            p = round(price / divisor, 4)
            out[p] = out.get(p, 0) + count
    return out


def route_quote(book, side):
    bids = levels(book, side)
    asks = {round(1 - p, 4): qty for p, qty in levels(book, "no" if side == "yes" else "yes").items()}
    bid, ask = max(bids, default=0), min(asks, default=1)
    return {"bid": bid, "ask": ask, "spread": round(ask - bid, 4), "asks": asks}


def outcome_routes(pair, books):
    outcomes = []
    for selected, opponent in (pair, pair[::-1]):
        routes = []
        for contract, side in [(selected, "yes"), (opponent, "no")]:
            ticker = contract["ticker"]
            if ticker in books:
                quote = route_quote(books[ticker], side)
                if 0 < quote["ask"] < 1 and quote["spread"] >= 0:
                    routes.append({**quote, "ticker": ticker, "side": side})
        routes.sort(key=lambda r: (r["ask"], r["spread"], r["side"] != "yes"))
        outcomes.append({"selected": selected, "opponent": opponent, "routes": routes})
    return outcomes


def underdog(pair, books):
    outcomes = outcome_routes(pair, books)
    dogs = [o for o in outcomes if o["routes"] and o["routes"][0]["ask"] < .5]
    # Both below 50 cents indicates crossed/ambiguous pricing, not a clear favorite.
    return dogs[0] if len(dogs) == 1 else None


def fee_for(count, price, multiplier=1):
    amount = Decimal("0.07") * Decimal(str(multiplier)) * count * Decimal(str(price)) * (1 - Decimal(str(price)))
    return float(amount.quantize(Decimal(".01"), rounding=ROUND_CEILING))


def simulated_fill(first, second, budget, max_price, multiplier):
    """Only depth still displayed a second later can fill. No queue or last-price fills."""
    common = {p: int(min(q, second.get(p, 0))) for p, q in first.items() if p <= max_price and p < .5}
    fills, premium, fees, count = [], 0.0, 0.0, 0
    for p in sorted(common):
        available = common[p]
        low, high = 0, available
        while low < high:
            n = (low + high + 1) // 2
            if premium + fees + n * p + fee_for(n, p, multiplier) <= budget + 1e-8:
                low = n
            else:
                high = n - 1
        if low:
            fee = fee_for(low, p, multiplier)
            fills.append({"price": p, "contracts": low, "fee": fee})
            premium += low * p
            fees += fee
            count += low
    total = round(premium + fees, 4)
    # Thin books do not turn a requested 1U wager into an undocumented tiny fill.
    rounding_room = max((r["price"] + fee_for(1, r["price"], multiplier) for r in fills), default=0)
    if not count or budget - total >= rounding_room + 1e-8:
        return None
    return {"contracts": count, "premium": round(premium, 4), "fee": round(fees, 4),
            "cost": total, "entry_price": round(premium / count, 6), "fills": fills,
            "target_units": 1, "actual_units": round(total / budget, 6)}


def settlement_value(market):
    if market.get("status") not in {"settled", "finalized"} or market.get("is_provisional") is True:
        return None
    explicit = market.get("settlement_value_dollars")
    if explicit is not None and explicit != "":
        value = number(explicit)
        return value if value is not None and 0 <= value <= 1 else None
    if market.get("settlement_value") is not None:
        value = number(market["settlement_value"])
        return value / 100 if value is not None and 0 <= value <= 100 else None
    return {"yes": 1.0, "no": 0.0}.get(market.get("result"))


class Experiment:
    def __init__(self, path, *, unit_dollars=None, now=None):
        self.path = Path(path)
        now = now or now_central()
        if self.path.exists() or Path(str(self.path) + ".gz").exists():
            self.state = read_json(self.path)
            self.validate()
        else:
            unit = number(unit_dollars)
            if unit is None or not 1 <= unit <= 10000:
                raise ValueError("A finite, explicit shadow unit of $1–$10000 is required for a new experiment")
            self.state = {"version": VERSION, "rules_hash": RULES_HASH, "mode": "shadow",
                          "started_at": stamp(now), "ends_at": stamp(now.astimezone(timezone.utc) + timedelta(days=7)),
                          "unit_dollars": round(unit, 2), "positions": {}, "history": {},
                          "scan_count": 0, "last_scan_at": None, "rejections": {},
                          "coverage": {}, "rules": RULES}

    def validate(self):
        s = self.state
        if s.get("version") != VERSION or s.get("rules_hash") != RULES_HASH or s.get("mode") != "shadow":
            raise ValueError("ITF experiment rules changed; preserve the existing cohort")
        start, end, unit = parsed(s.get("started_at")), parsed(s.get("ends_at")), number(s.get("unit_dollars"))
        if not start or not end or end - start != timedelta(days=7) or unit is None or not 1 <= unit <= 10000:
            raise ValueError("Invalid ITF experiment window or unit; refusing to reset state")
        if not isinstance(s.get("positions"), dict) or not isinstance(s.get("history"), dict):
            raise ValueError("Unreadable ITF ledger; refusing to reset state")
        for key, row in s["positions"].items():
            if (key != row.get("strategy", "") + "|" + row.get("event_ticker", "")
                    or row.get("strategy") not in STRATEGIES or row.get("mode") != "shadow"
                    or market_series(row.get("ticker")) not in SERIES
                    or row.get("side") not in {"yes", "no"}
                    or row.get("status") not in {"open", "settled"}
                    or number(row.get("contracts"), 0) <= 0 or number(row.get("cost"), 0) <= 0):
                raise ValueError("Invalid ITF position; refusing to reconstruct ledger")

    def persist(self):
        self.validate()
        atomic_json(self.path, self.state)

    def status(self, now):
        if now < parsed(self.state["started_at"]):
            return "clock_error"
        if now < parsed(self.state["ends_at"]):
            return "collecting"
        return "settling" if any(p["status"] == "open" for p in self.state["positions"].values()) else "complete"

    def reject(self, reason):
        counts = self.state["rejections"]
        counts[reason] = counts.get(reason, 0) + 1

    def observe(self, pair, first_books, second_books, context, multiplier, now):
        if self.status(now) != "collecting":
            return []
        times = [parsed(context.get(k)) for k in ("first_at", "confirmed_at", "status_at")]
        if (not all(times) or not 1 <= (times[1] - times[0]).total_seconds() <= 30
                or not 0 <= (now - times[0]).total_seconds() <= 30
                or not 0 <= (now - times[1]).total_seconds() <= 15
                or not 0 <= (now - times[2]).total_seconds() <= 15):
            self.reject("stale_or_unconfirmed_snapshot")
            return []
        if context.get("phase") not in {"pregame", "live"}:
            self.reject("match_" + str(context.get("phase") or "unknown"))
            return []
        first, second = underdog(pair, first_books), underdog(pair, second_books)
        if not first or not second or first["selected"]["ticker"] != second["selected"]["ticker"]:
            self.reject("no_unambiguous_underdog")
            return []
        selected = second["selected"]
        event = selected["event_ticker"]
        history_key = selected["ticker"]
        history = self.state["history"].setdefault(history_key, [])
        route = second["routes"][0]
        prior = [h for h in history if parsed(h["at"]) and 0 < (now - parsed(h["at"])).total_seconds() <= 1800]
        observation = {"at": stamp(now), "bid": route["bid"], "ask": route["ask"],
                       "phase": context["phase"], "ticker": route["ticker"], "side": route["side"]}
        self.state["history"][history_key] = (prior + [observation])[-61:]
        entries = []
        for strategy, rule in STRATEGIES.items():
            key = strategy + "|" + event
            if key in self.state["positions"]:
                continue
            selected_route = None
            for candidate in second["routes"]:
                original = next((r for r in first["routes"] if r["ticker"] == candidate["ticker"] and r["side"] == candidate["side"]), None)
                if not original:
                    continue
                if not all(rule["min_price"] <= r["ask"] <= rule["max_price"]
                           and r["spread"] <= rule.get("max_spread", 1) + 1e-8 for r in (original, candidate)):
                    continue
                if rule.get("momentum"):
                    same = [h for h in prior if h["ticker"] == candidate["ticker"] and h["side"] == candidate["side"]
                            and h["phase"] == context["phase"]]
                    anchors = [h for h in same if 300 <= (now - parsed(h["at"])).total_seconds() <= 1800]
                    recent = [h for h in same if 30 <= (now - parsed(h["at"])).total_seconds() <= 180]
                    # Oldest eligible point is fixed prospectively, never the retrospectively best trough.
                    if not anchors or not recent or not all(b >= anchors[0]["bid"] + .03 - 1e-8
                                                           for b in [original["bid"], candidate["bid"], recent[-1]["bid"]]):
                        continue
                fill = simulated_fill(original["asks"], candidate["asks"], self.state["unit_dollars"], rule["max_price"], multiplier)
                if fill:
                    selected_route = (candidate, fill)
                    break
            if not selected_route:
                self.reject(strategy + ":price_spread_history_or_depth")
                continue
            candidate, fill = selected_route
            row = {**fill, "id": key, "mode": "shadow", "strategy": strategy,
                   "event_ticker": event, "ticker": candidate["ticker"], "side": candidate["side"],
                   "selected_ticker": selected["ticker"], "selected": selected["yes_sub_title"],
                   "opponent": second["opponent"]["yes_sub_title"], "series": market_series(event),
                   "status": "open", "placed_at": stamp(now), "phase": context["phase"],
                   "scheduled_start": context.get("scheduled_start"), "tournament": context.get("tournament"),
                   "fee_multiplier": multiplier, "entry_bid": candidate["bid"], "entry_ask": candidate["ask"],
                   "context": context, "rules_hash": RULES_HASH,
                   "momentum_evidence": prior if rule.get("momentum") else []}
            self.state["positions"][key] = row
            entries.append(row)
        return entries

    def settle(self, markets, now):
        settled = []
        for row in self.state["positions"].values():
            if row["status"] != "open":
                continue
            market = markets.get(row["ticker"], {})
            if market.get("ticker") != row["ticker"]:
                continue
            value = settlement_value(market)
            if value is None:
                continue
            payout_per_contract = value if row["side"] == "yes" else 1 - value
            payout = round(row["contracts"] * payout_per_contract, 4)
            row.update({"status": "settled", "settled_at": stamp(now), "payout": payout,
                        "profit": round(payout - row["cost"], 4), "settlement_value": payout_per_contract,
                        "exchange_settlement_at": market.get("settlement_ts"),
                        "result": "WIN" if payout_per_contract == 1 else "LOSS" if payout_per_contract == 0 else "NON_BINARY"})
            settled.append(row)
        return settled

    def finish_scan(self, coverage, now):
        self.state["scan_count"] += 1
        self.state["last_scan_at"] = stamp(now)
        self.state["coverage"] = coverage
        self.state["history"] = {k: [h for h in rows if parsed(h["at"]) and 0 <= (now - parsed(h["at"])).total_seconds() <= 1800]
                                 for k, rows in self.state["history"].items()}
        self.state["history"] = {k: v for k, v in self.state["history"].items() if v}

    def summary(self, now=None):
        now = now or now_central()
        s, unit = self.state, self.state["unit_dollars"]
        positions = list(s["positions"].values())
        summaries = []
        for key, rule in STRATEGIES.items():
            rows = [r for r in positions if r["strategy"] == key]
            closed = sorted([r for r in rows if r["status"] == "settled"], key=lambda r: (r["settled_at"], r["id"]))
            wins = sum(r["result"] == "WIN" for r in closed)
            losses = sum(r["result"] == "LOSS" for r in closed)
            pnl, peak, drawdown = 0, 0, 0
            # Batch ties so the drawdown does not depend on dictionary ordering.
            pnl_at = defaultdict(float)
            for r in closed:
                pnl_at[r["settled_at"]] += r["profit"]
            for value in pnl_at.values():
                pnl += value
                peak = max(peak, pnl)
                drawdown = max(drawdown, peak - pnl)
            risk = sum(r["cost"] for r in rows if r["status"] == "open")
            spent = sum(r["cost"] for r in closed)
            segments = []
            for field in ("phase", "series"):
                for value in sorted({r[field] for r in rows}):
                    cohort = [r for r in rows if r[field] == value]
                    done = [r for r in cohort if r["status"] == "settled"]
                    segments.append({"field": field, "value": SERIES.get(value, value), "entries": len(cohort),
                                     "settled": len(done), "profit_units": round(sum(r["profit"] for r in done) / unit, 4)})
            summaries.append({"id": key, **rule, "entries": len(rows), "open": len(rows) - len(closed),
                              "settled": len(closed), "wins": wins, "losses": losses,
                              "non_binary": len(closed) - wins - losses,
                              "win_rate": round(100 * wins / (wins + losses), 2) if wins + losses else None,
                              "profit": round(pnl, 4), "profit_units": round(pnl / unit, 4),
                              "roi_percent": round(100 * pnl / spent, 2) if spent else None,
                              "open_risk_units": round(risk / unit, 4), "drawdown_units": round(drawdown / unit, 4),
                              "worst_case_units": round((pnl - risk) / unit, 4), "segments": segments})
        return {"version": VERSION, "mode": "shadow", "status": self.status(now), "generated_at": stamp(now),
                "started_at": s["started_at"], "ends_at": s["ends_at"], "unit_dollars": unit,
                "scan_count": s["scan_count"], "last_scan_at": s["last_scan_at"],
                "coverage": s["coverage"], "rejections": s["rejections"], "strategies": summaries,
                "positions": sorted(positions, key=lambda r: r["placed_at"], reverse=True)[:200],
                "position_count": len(positions), "rules_hash": RULES_HASH,
                "note": "Separate hypothetical portfolios; overlapping picks are correlated. Fees included. Whole-contract rounding can leave less than 1U. No live orders or automatic promotion."}


def dashboard_summary(root=None, now=None):
    root = Path(root or Path(__file__).resolve().parent)
    now = now or now_central()
    try:
        report = read_json(root / "sports_itf_shadow_report.json")
    except FileNotFoundError:
        return {"mode": "shadow", "status": "not_started", "health": "not_started", "strategies": []}
    except (OSError, ValueError):
        return {"mode": "shadow", "status": "unreadable", "health": "error", "strategies": []}
    last = parsed(report.get("generated_at"))
    age = (now - last).total_seconds() if last else None
    report["age_seconds"] = round(age, 1) if age is not None else None
    report["health"] = ("error" if report.get("worker", {}).get("error") else
                        "complete" if report.get("status") == "complete" else
                        "healthy" if age is not None and 0 <= age <= 180 else "stale")
    return report
