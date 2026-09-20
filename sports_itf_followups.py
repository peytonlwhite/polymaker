"""Registered, shadow-only subset experiments over the frozen ITF ledger.

No network, credentials, live portfolio, or order interface. These portfolios
reuse parent simulated fills; they do not assume additional executable depth.
"""
import copy
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from itf_shadow_accounting import position_accounting
from sports_itf_shadow import CENTRAL, atomic_json, now_central, parsed, read_json, stamp

VERSION = "itf-followups-v1"
HYPOTHESES = {
    "men_doubles_live": {
        "name": "Men's doubles · first entry live", "parent": "all_underdogs",
        "series": "KXITFDOUBLES", "phase": "live",
        "description": "Men's doubles where Every underdog first enters during live play; under 50 cents, original confirmed fills and fees.",
    },
    "women_live_strengthening": {
        "name": "Women's singles · live strengthening", "parent": "strengthening",
        "series": "KXITFWMATCH", "phase": "live",
        "description": "Women's singles where Strengthening dogs first enters live; 10–45 cents, spread at most 8 cents, original confirmed momentum rule.",
    },
}
RULES = {"version": VERSION, "hypotheses": HYPOTHESES,
         "entry": "parent simulated fills after registration; exclude every event already in parent positions",
         "size": "inherit original fixed 1U including fees", "exit": "inherit official parent settlement",
         "end": "original parent entry deadline", "minimum_review_matches": 100,
         "minimum_review_entry_days": 3, "automatic_promotion": False}
RULES_HASH = hashlib.sha256(json.dumps(RULES, sort_keys=True).encode()).hexdigest()
STATE = "sports_itf_followups_state.json"
REPORT = "sports_itf_followups_report.json"
SETTLEMENT_FIELDS = {"status", "settled_at", "payout", "profit", "settlement_value",
                     "exchange_settlement_at", "result"}


def matches(row, rule):
    # No outcome or profit field participates in selection.
    return (row.get("mode") == "shadow" and row.get("strategy") == rule["parent"]
            and row.get("series") == rule["series"] and row.get("phase") == rule["phase"])


def digest_entry(row):
    return hashlib.sha256(json.dumps({k: v for k, v in row.items() if k not in SETTLEMENT_FIELDS},
                                     sort_keys=True).encode()).hexdigest()


def source_identity(source):
    return {key: source[key] for key in ("version", "rules_hash", "implementation_hash",
                                       "started_at", "ends_at", "unit_dollars")}


def metrics(rows, unit, bootstrap=False):
    closed = sorted((r for r in rows if r["status"] == "settled"), key=lambda r: (r["placed_at"], r["id"]))
    costs = sum(r["cost"] for r in closed)
    profit = sum(r["profit"] for r in closed)
    risk = sum(r["cost"] for r in rows if r["status"] == "open")
    days = {parsed(r["placed_at"]).astimezone(CENTRAL).date().isoformat() for r in closed}
    batches = defaultdict(float)
    for row in closed:
        batches[row["settled_at"]] += row["profit"]
    pnl = peak = drawdown = 0
    for key in sorted(batches, key=parsed):
        pnl += batches[key]
        peak = max(peak, pnl)
        drawdown = max(drawdown, peak - pnl)
    result = {"entries": len(rows), "settled": len(closed), "open": len(rows) - len(closed),
              "unique_settled_matches": len({r["event_ticker"] for r in closed}),
              "entry_days": len(days), "wins": sum(r.get("result") == "WIN" for r in closed),
              "losses": sum(r.get("result") == "LOSS" for r in closed),
              "non_binary": sum(r.get("result") == "NON_BINARY" for r in closed),
              "profit_units": round(profit / unit, 4), "roi_percent": round(100 * profit / costs, 2) if costs else None,
              "open_risk_units": round(risk / unit, 4), "worst_case_units": round((profit - risk) / unit, 4),
              "drawdown_units": round(drawdown / unit, 4),
              "without_best_win_units": round((profit - max([0] + [r["profit"] for r in closed])) / unit, 4),
              "first_half_units": round(sum(r["profit"] for r in closed[:len(closed)//2]) / unit, 4),
              "second_half_units": round(sum(r["profit"] for r in closed[len(closed)//2:]) / unit, 4)}
    if bootstrap and len(closed) >= 2:
        # Descriptive, match-level intervals; not a multiple-testing correction.
        rng, samples = random.Random(1609), []
        for _ in range(3000):
            sample = rng.choices(closed, k=len(closed))
            samples.append(100 * sum(r["profit"] for r in sample) / sum(r["cost"] for r in sample))
        samples.sort()
        result["exploratory_roi_interval_95"] = [round(samples[75], 2), round(samples[2924], 2)]
    return result


def exploratory_analysis(source):
    """Freeze the complete broad segmentation inspected, not only winning slices."""
    rows = list(source["positions"].values())
    unit = source["unit_dollars"]
    groups = defaultdict(list)
    for row in rows:
        price, spread = row["entry_price"], row["entry_ask"] - row["entry_bid"]
        band = "under20c" if price < .2 else "20to35c" if price < .35 else "35to50c"
        tight = "up_to3c" if spread <= .030001 else "3to8c" if spread <= .080001 else "over8c"
        kind = "doubles" if "DOUBLES" in row["series"] else "singles"
        for label in ("overall", row["phase"], row["phase"] + ":" + kind,
                      row["phase"] + ":" + row["series"], row["phase"] + ":price:" + band,
                      row["phase"] + ":spread:" + tight):
            groups[(row["strategy"], label)].append(row)
    return [{"parent": parent, "segment": label, **metrics(values, unit)}
            for (parent, label), values in sorted(groups.items())]


class Followups:
    def __init__(self, root, source, *, now=None, register=False):
        self.root, now = Path(root), now or now_central()
        path = self.root / STATE
        try:
            self.state = read_json(path)
        except FileNotFoundError:
            if not register or (self.root / REPORT).exists():
                raise ValueError("No follow-up ledger; explicit first registration required, never reset")
            if now >= parsed(source["ends_at"]):
                raise ValueError("Parent entry window ended")
            if source.get("mode") != "shadow":
                raise ValueError("Source must be the shadow experiment")
            for row in source["positions"].values():
                self.check_accounting(row, source["unit_dollars"])
            self.state = {"version": VERSION, "mode": "shadow", "rules_hash": RULES_HASH,
                          "rules": copy.deepcopy(RULES), "source": source_identity(source),
                          "started_at": stamp(now), "ends_at": source["ends_at"],
                          "excluded_events": sorted({r["event_ticker"] for r in source["positions"].values()}),
                          "positions": {}, "scan_count": 0, "last_scan_at": None,
                          "discovery": exploratory_analysis(source),
                          "historical": {key: metrics([r for r in source["positions"].values() if matches(r, rule)],
                                                       source["unit_dollars"], bootstrap=True)
                                         for key, rule in HYPOTHESES.items()}}
        if (self.state.get("mode") != "shadow" or self.state.get("version") != VERSION
                or self.state.get("rules_hash") != RULES_HASH or self.state.get("rules") != RULES
                or self.state.get("source") != source_identity(source)
                or not isinstance(self.state.get("positions"), dict)):
            raise ValueError("Follow-up rules or source changed; preserve existing cohorts")
        if now < parsed(self.state["started_at"]):
            raise ValueError("Clock precedes registration")

    @staticmethod
    def check_accounting(row, unit):
        _, errors = position_accounting(row, unit)
        if errors:
            raise ValueError("Parent accounting mismatch: " + row["id"] + " " + ",".join(errors))

    def sync(self, source, now=None):
        now = now or now_central()
        if source_identity(source) != self.state["source"]:
            raise ValueError("Parent experiment identity changed")
        start, end = parsed(self.state["started_at"]), parsed(self.state["ends_at"])
        excluded = set(self.state["excluded_events"])
        next_positions = copy.deepcopy(self.state["positions"])
        for saved in next_positions.values():
            if saved["source_id"] not in source["positions"]:
                raise ValueError("Enrolled parent entry missing; preserve existing result")
        for source_id, row in source["positions"].items():
            placed = parsed(row.get("placed_at"))
            if not placed or not start <= placed < end or placed > now or row["event_ticker"] in excluded:
                continue
            for key, rule in HYPOTHESES.items():
                if not matches(row, rule):
                    continue
                self.check_accounting(row, source["unit_dollars"])
                identity = key + "|" + row["event_ticker"]
                entry_hash = digest_entry(row)
                previous = next_positions.get(identity)
                if previous:
                    if previous["entry_hash"] != entry_hash or previous["source_id"] != source_id:
                        raise ValueError("Parent entry changed after enrollment")
                    if previous["row"]["status"] == "settled" and previous["row"] != row:
                        raise ValueError("Parent settlement changed after enrollment")
                next_positions[identity] = {"hypothesis": key, "source_id": source_id,
                                            "entry_hash": entry_hash, "row": copy.deepcopy(row)}
        self.state.update(positions=next_positions, last_scan_at=stamp(now),
                          source_last_scan_at=source.get("last_scan_at"),
                          scan_count=self.state["scan_count"] + 1)

    def summary(self, now=None):
        now = now or now_central()
        s, unit = self.state, self.state["source"]["unit_dollars"]
        strategies = []
        for key, rule in HYPOTHESES.items():
            rows = [r["row"] for r in s["positions"].values() if r["hypothesis"] == key]
            values = metrics(rows, unit)
            enough = values["unique_settled_matches"] >= 100 and values["entry_days"] >= 3
            strategies.append({"id": key, **rule, **values, "historical": s["historical"][key],
                               "evidence": "Review sample reached; unconfirmed" if enough else "Insufficient new evidence"})
        parent_at = parsed(s.get("source_last_scan_at"))
        awaiting_parent = not parent_at or parent_at < parsed(s["ends_at"])
        status = ("collecting" if now < parsed(s["ends_at"]) else "settling"
                  if awaiting_parent or any(r["open"] for r in strategies) else "complete")
        return {"version": VERSION, "mode": "shadow", "status": status, "started_at": s["started_at"],
                "ends_at": s["ends_at"], "unit_dollars": unit, "last_scan_at": s["last_scan_at"],
                "source_last_scan_at": s.get("source_last_scan_at"), "generated_at": stamp(now),
                "rules_hash": RULES_HASH, "excluded_event_count": len(s["excluded_events"]),
                "scan_count": s["scan_count"], "strategies": strategies,
                "note": "Historical selections are exploratory. New results start at registration and exclude previously observed matches. Parent fills are reused; overlapping portfolios are not independent."}

    def persist(self):
        atomic_json(self.root / STATE, self.state)


def dashboard_summary(root=None, now=None):
    root, now = Path(root or Path(__file__).resolve().parent), now or now_central()
    try:
        report = read_json(root / REPORT)
        timestamps = [parsed(report.get(k)) for k in ("generated_at", "source_last_scan_at")]
        fresh = all(t and 0 <= (now - t).total_seconds() <= 180 for t in timestamps)
        report["health"] = ("error" if report.get("worker", {}).get("error") else "complete"
                            if report["status"] == "complete" else "healthy" if fresh else "stale")
        return report
    except FileNotFoundError:
        return {"health": "not_started", "strategies": []}
    except (ValueError, OSError, KeyError, TypeError):
        return {"health": "error", "strategies": []}
