"""Prospective ITF price-quality comparison. Isolated virtual portfolios only."""
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta, timezone
import hashlib
import json
from pathlib import Path

from itf_shadow_accounting import position_accounting
from sports_itf_followups import exploratory_analysis, metrics, source_identity
from sports_itf_shadow import (Experiment, atomic_json, fee_for, market_series,
                               now_central, number, outcome_routes, pair_markets,
                               parsed, read_json, stamp, underdog)

VERSION = "itf-price-quality-v1"
STATE = "sports_itf_price_quality_state.json"
REPORT = "sports_itf_price_quality_report.json"
STRATEGIES = {
    "paired_dog": {"name": "Tight-spread underdog control", "min_price": .20, "max_price": .45,
                   "description": "Pregame 20–45c underdog; matched against a favorite on the same match."},
    "paired_favorite": {"name": "Tight-spread favorite", "min_price": .55, "max_price": .80,
                        "description": "Pregame 55–80c favorite; same matches and entry times as the control."},
    "live_favorite_momentum": {"name": "Live strengthening favorite", "min_price": .55, "max_price": .80,
                               "description": "Live 55–80c favorite; bid rises 3c over 5–30 minutes and stays confirmed."},
}
RULES = {"version": VERSION, "strategies": STRATEGIES, "days": 7,
         "unit_dollars": 5.0, "bankroll_dollars": 5000.0, "max_spread": .04,
         "max_open_risk_dollars": 100.0, "max_tournament_risk_dollars": 25.0,
         "stop_drawdown_dollars": 250.0, "fixed_fee_inclusive_stakes": True,
         "minimum_review_matches": 200, "minimum_review_entry_days": 5,
         "confirmation_seconds": [1, 30], "first_max_age_seconds": 30,
         "confirmed_max_age_seconds": 15, "automatic_live_promotion": False,
         "pregame_pair": "Both sides independently executable; enter both or neither in separate portfolios",
         "new_evidence": "Exclude every event already held by the original experiment at registration",
         "momentum": "Oldest same-route live anchor 5–30min old, prior confirmation 30–180s old, both current bids +3c",
         "fill": "Whole contracts, minimum depth at identical prices across two snapshots; quadratic taker fees per level",
         "settlement": "Official non-provisional settlement, including fractional payouts; no exits"}
RULES_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()


def fill_at_asks(first, second, budget, max_price, multiplier):
    """Conservative marketable fill, including favorites above 50c."""
    if number(multiplier) is None or multiplier < 0:
        return None
    count, premium, fees, fills = 0, 0.0, 0.0, []
    for price in sorted(first):
        if not 0 < price <= max_price < 1:
            continue
        quantity = int(min(first[price], second.get(price, 0)))
        low, high = 0, max(0, quantity)
        while low < high:
            n = (low + high + 1) // 2
            if premium + fees + n * price + fee_for(n, price, multiplier) <= budget + 1e-8:
                low = n
            else:
                high = n - 1
        if low:
            fee = fee_for(low, price, multiplier)
            fills.append({"price": price, "contracts": low, "fee": fee})
            premium += low * price
            fees += fee
            count += low
    cost = round(premium + fees, 4)
    rounding_room = max((r["price"] + fee_for(1, r["price"], multiplier) for r in fills), default=0)
    if not count or budget - cost >= rounding_room + 1e-8:
        return None
    return {"contracts": count, "premium": round(premium, 4), "fee": round(fees, 4),
            "cost": cost, "entry_price": round(premium / count, 6), "fills": fills,
            "target_units": 1, "actual_units": round(cost / budget, 6)}


class PriceQuality(Experiment):
    """Reuse the original settlement and snapshot bookkeeping, never its ledger."""
    def __init__(self, root, *, now=None, register=False, source=None):
        self.root = Path(root)
        self.path = self.root / STATE
        now = now or now_central()
        try:
            self.state = read_json(self.path)
        except FileNotFoundError:
            if (not register or (self.root / REPORT).exists() or (self.root / (REPORT + ".gz")).exists()
                    or any((self.root / "archives" / "sports_itf_price_quality").glob("*.jsonl*"))):
                raise ValueError("Explicit first registration required; missing state is never reset")
            if not source or source.get("mode") != "shadow":
                raise ValueError("A validated original shadow ledger is required")
            source_at = parsed(source.get("last_scan_at"))
            if not source_at or not 0 <= (now - source_at).total_seconds() <= 180:
                raise ValueError("Registration requires a fresh original shadow ledger")
            for row in source["positions"].values():
                if position_accounting(row, source["unit_dollars"])[1]:
                    raise ValueError("Original ledger accounting failed")
            self.state = {"version": VERSION, "mode": "shadow", "rules_hash": RULES_HASH,
                          "rules": deepcopy(RULES), "unit_dollars": RULES["unit_dollars"],
                          "bankroll_dollars": RULES["bankroll_dollars"],
                          "started_at": stamp(now), "ends_at": stamp(now.astimezone(timezone.utc) + timedelta(days=7)),
                          "positions": {}, "history": {}, "scan_count": 0, "last_scan_at": None,
                          "coverage": {}, "rejections": {}, "entry_stops": {},
                          "source": source_identity(source), "discovery_at": source["last_scan_at"],
                          "excluded_events": sorted({r["event_ticker"] for r in source["positions"].values()}),
                          "discovery": exploratory_analysis(source)}
        self.validate()
        if now < parsed(self.state["started_at"]):
            raise ValueError("Clock precedes registration")

    def validate(self):
        s = self.state
        if (s.get("version") != VERSION or s.get("mode") != "shadow" or s.get("rules_hash") != RULES_HASH
                or s.get("rules") != RULES or s.get("unit_dollars") != RULES["unit_dollars"]
                or s.get("bankroll_dollars") != RULES["bankroll_dollars"]):
            raise ValueError("Price-quality protocol changed; preserve frozen cohort")
        start, end = parsed(s.get("started_at")), parsed(s.get("ends_at"))
        if not start or not end or end.astimezone(timezone.utc) - start.astimezone(timezone.utc) != timedelta(days=7):
            raise ValueError("Invalid registered entry window")
        if (not isinstance(s.get("positions"), dict) or not isinstance(s.get("history"), dict)
                or not isinstance(s.get("entry_stops"), dict) or not isinstance(s.get("excluded_events"), list)):
            raise ValueError("Invalid experiment state; refusing reconstruction")
        for key, row in s["positions"].items():
            at = parsed(row.get("placed_at"))
            rule = STRATEGIES.get(row.get("strategy"))
            if (not rule or key != row["strategy"] + "|" + row.get("event_ticker", "")
                    or row.get("rules_hash") != RULES_HASH or row.get("mode") != "shadow"
                    or not at or not start <= at < end or row["event_ticker"] in s["excluded_events"]
                    or row.get("side") not in {"yes", "no"} or row.get("status") not in {"open", "settled"}
                    or not market_series(row.get("ticker")) or row.get("phase") not in {"pregame", "live"}
                    or row.get("contracts") != int(row.get("contracts", 0))
                    or not 0 < number(row.get("cost"), 0) <= s["unit_dollars"] + 1e-8
                    or position_accounting(row, s["unit_dollars"])[1]):
                raise ValueError("Invalid price-quality position or accounting")
            if (any(not rule["min_price"] - 1e-8 <= number(f.get("price"), -1) <= rule["max_price"] + 1e-8
                    or not isinstance(f.get("contracts"), int) or f["contracts"] <= 0
                    or f["fee"] != fee_for(f["contracts"], f["price"], row["fee_multiplier"])
                    for f in row["fills"])
                    or row["phase"] != ("live" if row["strategy"] == "live_favorite_momentum" else "pregame")):
                raise ValueError("Position violates registered fill or phase rules")
        dogs = {r["event_ticker"]: r for r in s["positions"].values() if r["strategy"] == "paired_dog"}
        favorites = {r["event_ticker"]: r for r in s["positions"].values() if r["strategy"] == "paired_favorite"}
        if dogs.keys() != favorites.keys() or any(dogs[e]["placed_at"] != favorites[e]["placed_at"] for e in dogs):
            raise ValueError("Matched comparison is incomplete")

    def risk_allows(self, strategy, tournament, now):
        rows = [r for r in self.state["positions"].values() if r["strategy"] == strategy]
        values = metrics(rows, self.state["unit_dollars"])
        if values["drawdown_units"] * self.state["unit_dollars"] >= RULES["stop_drawdown_dollars"] - 1e-8:
            self.state["entry_stops"].setdefault(strategy, stamp(now))
        if strategy in self.state["entry_stops"]:
            return False
        opened = [r for r in rows if r["status"] == "open"]
        risk = sum(r["cost"] for r in opened)
        tournament_risk = sum(r["cost"] for r in opened if r.get("tournament") == tournament)
        equity = self.state["bankroll_dollars"] + sum(r["profit"] for r in rows if r["status"] == "settled")
        return (risk + self.state["unit_dollars"] <= min(equity, RULES["max_open_risk_dollars"]) + 1e-8
                and tournament_risk + self.state["unit_dollars"] <= RULES["max_tournament_risk_dollars"] + 1e-8)

    def observe(self, pair, first_books, second_books, context, multiplier, now):
        if self.status(now) != "collecting":
            return []
        if len(pair_markets(pair)) != 1:
            self.reject("invalid_pair")
            return []
        event = pair[0]["event_ticker"]
        if event in self.state["excluded_events"]:
            self.reject("previously_observed_event")
            return []
        times = [parsed(context.get(k)) for k in ("first_at", "confirmed_at", "status_at")]
        if (not all(times) or not 1 <= (times[1] - times[0]).total_seconds() <= 30
                or not 0 <= (now - times[0]).total_seconds() <= 30
                or not 0 <= (now - times[1]).total_seconds() <= 15
                or not 0 <= (now - times[2]).total_seconds() <= 15):
            self.reject("stale_or_unconfirmed_snapshot")
            return []
        phase = context.get("phase")
        if phase not in {"pregame", "live"}:
            self.reject("unknown_or_finished_match")
            return []
        start = parsed(context.get("scheduled_start"))
        if phase == "pregame" and (not start or start <= now):
            self.reject("missing_or_overdue_scheduled_start")
            return []
        dog1, dog2 = underdog(pair, first_books), underdog(pair, second_books)
        if not dog1 or not dog2 or dog1["selected"]["ticker"] != dog2["selected"]["ticker"]:
            self.reject("ambiguous_or_changed_favorite")
            return []
        first = {o["selected"]["ticker"]: o for o in outcome_routes(pair, first_books)}
        second = {o["selected"]["ticker"]: o for o in outcome_routes(pair, second_books)}
        favorite = second.get(dog2["opponent"]["ticker"])
        if not favorite or favorite["selected"]["ticker"] not in first:
            self.reject("missing_favorite_book")
            return []
        prior = []
        if phase == "live":
            history_key = favorite["selected"]["ticker"]
            prior = [h for h in self.state["history"].get(history_key, [])
                     if 0 < (now - parsed(h["at"])).total_seconds() <= 1800]
            observations = [{"at": stamp(now), "phase": phase, **{k: r[k] for k in ("ticker", "side", "bid", "ask")}}
                            for r in favorite["routes"]]
            self.state["history"][history_key] = (prior + observations)[-122:]
        choices = [("paired_dog", dog2), ("paired_favorite", favorite)] if phase == "pregame" else [("live_favorite_momentum", favorite)]
        entries = []
        for strategy, outcome in choices:
            key = strategy + "|" + event
            if key in self.state["positions"]:
                return []
            if not self.risk_allows(strategy, context.get("tournament"), now):
                self.reject(strategy + ":bankroll_or_exposure_stop")
                return []
            rule = STRATEGIES[strategy]
            entry = None
            for route in outcome["routes"]:
                initial = next((r for r in first[outcome["selected"]["ticker"]]["routes"]
                                if r["ticker"] == route["ticker"] and r["side"] == route["side"]), None)
                if not initial or not all(rule["min_price"] <= r["ask"] <= rule["max_price"]
                                          and r["spread"] <= RULES["max_spread"] + 1e-8 for r in (initial, route)):
                    continue
                evidence = []
                if phase == "live":
                    same = [h for h in prior if h["ticker"] == route["ticker"] and h["side"] == route["side"]]
                    anchors = [h for h in same if 300 <= (now - parsed(h["at"])).total_seconds() <= 1800]
                    recent = [h for h in same if 30 <= (now - parsed(h["at"])).total_seconds() <= 180]
                    if not anchors or not recent or not all(b >= anchors[0]["bid"] + .03 - 1e-8
                                                           for b in (initial["bid"], route["bid"], recent[-1]["bid"])):
                        continue
                    evidence = [anchors[0], recent[-1]]
                fill = fill_at_asks(initial["asks"], route["asks"], self.state["unit_dollars"], rule["max_price"], multiplier)
                if fill:
                    entry = {**fill, "id": key, "mode": "shadow", "strategy": strategy,
                             "event_ticker": event, "ticker": route["ticker"], "side": route["side"],
                             "selected_ticker": outcome["selected"]["ticker"], "selected": outcome["selected"]["yes_sub_title"],
                             "opponent": outcome["opponent"]["yes_sub_title"], "series": market_series(event),
                             "status": "open", "placed_at": stamp(now), "phase": phase,
                             "scheduled_start": context.get("scheduled_start"), "tournament": context.get("tournament"),
                             "fee_multiplier": multiplier, "entry_bid": route["bid"], "entry_ask": route["ask"],
                             "context": deepcopy(context), "rules_hash": RULES_HASH, "momentum_evidence": evidence,
                             "fill_evidence": {"first": deepcopy(initial), "confirmed": deepcopy(route)}}
                    break
            if not entry:
                self.reject(strategy + ":price_spread_momentum_or_depth")
                return []
            entries.append(entry)
        # Paired entries are committed together only after both fills and risk gates pass.
        self.state["positions"].update({row["id"]: row for row in entries})
        return entries

    def settle(self, markets, now):
        settled = super().settle(markets, now)
        for strategy in STRATEGIES:
            self.risk_allows(strategy, None, now)
        return settled

    def summary(self, now=None):
        now, s = now or now_central(), self.state
        strategies = []
        for key, rule in STRATEGIES.items():
            rows = [r for r in s["positions"].values() if r["strategy"] == key]
            values = metrics(rows, s["unit_dollars"])
            enough = values["unique_settled_matches"] >= 200 and values["entry_days"] >= 5
            cash = s["bankroll_dollars"] + sum(r.get("profit", 0) for r in rows if r["status"] == "settled") - sum(r["cost"] for r in rows if r["status"] == "open")
            strategies.append({"id": key, **rule, **values, "available_cash": round(cash, 4),
                               "entries_stopped_at": s["entry_stops"].get(key),
                               "evidence": "Review sample reached; still unproven" if enough else "Insufficient new evidence"})
        paired = defaultdict(dict)
        for row in s["positions"].values():
            if row["strategy"] in {"paired_dog", "paired_favorite"}:
                paired[row["event_ticker"]][row["strategy"]] = row
        completed = [rows for rows in paired.values() if len(rows) == 2 and all(r["status"] == "settled" for r in rows.values())]
        gap = sum(rows["paired_favorite"]["profit"] - rows["paired_dog"]["profit"] for rows in completed)
        return {"version": VERSION, "mode": "shadow", "status": self.status(now), "generated_at": stamp(now),
                "started_at": s["started_at"], "ends_at": s["ends_at"], "last_scan_at": s["last_scan_at"],
                "unit_dollars": s["unit_dollars"], "bankroll_dollars": s["bankroll_dollars"],
                "rules_hash": RULES_HASH, "rules": RULES, "scan_count": s["scan_count"],
                "excluded_event_count": len(s["excluded_events"]), "coverage": s["coverage"],
                "rejections": s["rejections"], "strategies": strategies, "position_count": len(s["positions"]),
                "paired_comparison": {"completed_matches": len(completed), "pending_matches": len(paired) - len(completed),
                                      "favorite_minus_dog_profit_units": round(gap / s["unit_dollars"], 4)},
                "positions": sorted(s["positions"].values(), key=lambda r: (r["placed_at"], r["id"]), reverse=True),
                "note": "Independent $5,000 virtual portfolios; fixed $5 fee-inclusive stakes. Paired sides are alternative simulations. New matches only; no historical favorite returns inferred; no automatic live promotion."}


def dashboard_summary(root=None, now=None):
    root, now = Path(root or Path(__file__).resolve().parent), now or now_central()
    try:
        report = read_json(root / REPORT)
        times = [parsed(report.get(k)) for k in ("generated_at", "last_scan_at")]
        fresh = all(t and 0 <= (now - t).total_seconds() <= 180 for t in times)
        report["health"] = ("error" if report.get("worker", {}).get("error") else "complete"
                            if report["status"] == "complete" else "healthy" if fresh else "stale")
        return report
    except FileNotFoundError:
        return {"health": "not_started", "strategies": []}
    except (ValueError, OSError, KeyError, TypeError):
        return {"health": "error", "strategies": []}
