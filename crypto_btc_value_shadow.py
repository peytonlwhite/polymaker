"""Prospective BTC price-quality experiments; public data and simulated fills only."""
from copy import deepcopy
from datetime import timedelta
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import secrets

from crypto_btc_random_shadow import (RandomCycleLab, btc_window, order_cost,
    american_odds, utcnow, next_slot, local_time, local_day, CHICAGO)
from crypto_execution_safety import confirmation_reasons
from crypto_market_regime import number, parse_time

VERSION = "btc-value-shadow-v1"
ARMS = {"balanced_value": "Balanced random / value",
        "balanced_patient": "Balanced random / patient value",
        "forecast_fade_value": "Forecast fade / value"}
POLICY = {"version": VERSION, "arms": ARMS, "bankroll": 5000., "base_unit": 5.,
          "minimum_price_cents": 100/3, "maximum_price_cents": 49.,
          "maximum_spread_cents": 2., "maximum_quote_age_seconds": 2,
          "entry_seconds": 300, "preselect_seconds": 60,
          "patient_seconds": 60, "patient_improvement_cents": 1.,
          "gross_daily_loss_limit": 50., "drawdown_pause": 250., "drawdown_reduce": 100.,
          "duration_days": 30, "review_settled_markets": 200, "review_entry_days": 10,
          "automatic_promotion": False, "live_orders": False,
          "balanced_deck": "four_up_four_down_per_eight_scheduled_windows",
          "pair_requires_both_flat": True, "forecast_max_age_seconds": 90,
          "forecast_direction": "opposite_fresh_non_neutral_prestart_forecast"}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()


def read_json(path):
    path = Path(path)
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    return json.loads(data.decode("utf-8-sig"))


def discovery_snapshot(source):
    """Descriptive discovery evidence only; never imported as new-cohort trades."""
    results = {}
    for arm in sorted({r["arm"] for r in source.get("records", [])}):
        trades = [r for r in source["records"] if r["arm"] == arm and r["status"] == "settled"]
        groups = {"all": trades,
                  "price_below_40": [r for r in trades if r["entry_price"] < 40],
                  "price_40_to_below_50": [r for r in trades if 40 <= r["entry_price"] < 50],
                  "price_50_and_above": [r for r in trades if r["entry_price"] >= 50]}
        for side in ("yes", "no"):
            groups["side_"+side] = [r for r in trades if r["side"] == side]
        for label, lower, upper in (("first_five_minutes", 0, 300), ("middle_five_minutes", 300, 600), ("last_five_minutes", 600, 900)):
            groups[label] = [r for r in trades if lower <= (parse_time(r["entered_at"])-parse_time(r["start_at"])).total_seconds() < upper]
        for day in sorted({local_day(r["entered_at"]) for r in trades}):
            groups["entry_day_"+day] = [r for r in trades if local_day(r["entered_at"]) == day]
        results[arm] = {key: {"settled": len(rows), "wins": sum(r["profit"] > 0 for r in rows),
                            "profit": round(sum(r["profit"] for r in rows), 6),
                            "stress_profit": round(sum(r["stress_profit"] for r in rows), 6)}
                        for key, rows in groups.items()}
    forecasts = [r for r in source.get("forecasts", []) if r.get("horizon") == "15m"
                 and r.get("status") == "resolved" and r.get("probability_up", .5) != .5]
    return {"source_version": source.get("version"), "source_policy_hash": source.get("policy_hash"),
            "source_sha256": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest(),
            "arms": results, "forecast_settled": len(forecasts),
            "forecast_correct": sum(int(r["probability_up"] > .5) == r["outcome"] for r in forecasts),
            "note": "Exploratory, overlapping small samples and multiple comparisons; not validation or evidence of a reliable edge."}


class ValueLab(RandomCycleLab):
    def __init__(self, path, now=None, register=False, source=None):
        self.path = Path(path)
        now = now or utcnow()
        if self.path.exists():
            self.state = read_json(self.path)
        elif register:
            if not isinstance(source, dict) or not isinstance(source.get("records"), list):
                raise ValueError("Registration requires the original discovery ledger")
            self.state = {"version": VERSION, "mode": "shadow_only", "policy_hash": POLICY_HASH,
                          "policy": deepcopy(POLICY), "seed": secrets.token_hex(32),
                          "registered_at": now.isoformat(), "ends_at": (now+timedelta(days=30)).isoformat(),
                          "records": [], "forecasts": [], "scan_count": 0, "rejections": {},
                          "discovery": discovery_snapshot(source)}
            self.persist()
        else:
            raise ValueError("Missing registered cohort; refusing to reset or register implicitly")
        self.audit()

    def audit(self):
        s = self.state
        if (s.get("version") != VERSION or s.get("mode") != "shadow_only"
                or s.get("policy_hash") != POLICY_HASH or s.get("policy") != POLICY
                or not isinstance(s.get("seed"), str) or len(s["seed"]) != 64):
            raise ValueError("Invalid or changed frozen shadow policy")
        registered, ends = parse_time(s.get("registered_at")), parse_time(s.get("ends_at"))
        if not registered or not ends or ends-registered != timedelta(days=POLICY["duration_days"]):
            raise ValueError("Invalid registered observation window")
        ids, opened = set(), {a: 0 for a in ARMS}
        records = s["records"]
        by_id = {r["id"]: r for r in records}
        for r in records:
            start, picked = parse_time(r["start_at"]), parse_time(r["picked_at"])
            if (r["id"] in ids or r["arm"] not in ARMS or r["side"] not in {"yes", "no"}
                    or r["status"] not in {"waiting", "missed", "filled", "settled"}
                    or not start or not picked or not registered <= picked < start < ends
                    or not 0 < (start-picked).total_seconds() <= 60 or start.timestamp() % 900
                    or r["id"] != f"{r['arm']}:{int(start.timestamp())}"
                    or parse_time(r["close_at"]) != start+timedelta(minutes=15)):
                raise ValueError("Invalid persisted prestart cycle")
            ids.add(r["id"])
            if r["arm"].startswith("balanced_"):
                paired_arm = "balanced_patient" if r["arm"] == "balanced_value" else "balanced_value"
                pair = by_id.get(f"{paired_arm}:{int(start.timestamp())}")
                if not pair or pair["side"] != r["side"] or pair["picked_at"] != r["picked_at"]:
                    raise ValueError("Broken paired random draw")
            if r["status"] not in {"filled", "settled"}:
                continue
            entered = parse_time(r["entered_at"])
            if (not entered or not start <= entered < min(ends, start+timedelta(seconds=300))
                    or r.get("live_order") is not False
                    or not isinstance(r["contracts"], int) or isinstance(r["contracts"], bool) or r["contracts"] < 1
                    or not 0 < r["cost"] <= POLICY["base_unit"]+1e-9
                    or not POLICY["minimum_price_cents"]-1e-9 <= r["entry_price"] <= 49
                    or not math.isfinite(r["fees"]) or r["fees"] < 0
                    or abs(r["entry_price"]*r["contracts"]/100+r["fees"]-r["cost"]) > 1e-6):
                raise ValueError("Invalid shadow fill accounting or timing")
            if r["arm"] == "balanced_patient":
                anchor = by_id[f"balanced_value:{int(start.timestamp())}"]
                if (anchor["status"] not in {"filled", "settled"}
                        or not parse_time(anchor["entered_at"]) < entered < parse_time(anchor["entered_at"])+timedelta(seconds=60)
                        or r["entry_price"] > anchor["entry_price"]-1+1e-9):
                    raise ValueError("Invalid patient fill anchor")
            if r["status"] == "filled":
                opened[r["arm"]] += 1
            elif (not math.isfinite(r["profit"]) or r["payout"] not in (0, r["contracts"])
                  or abs(r["profit"]-(r["payout"]-r["cost"])) > 1e-6
                  or abs(r["stress_profit"]-(r["profit"]-.01*r["contracts"])) > 1e-6):
                raise ValueError("Invalid settlement accounting")
        if any(n > 1 for n in opened.values()):
            raise ValueError("Overlapping shadow positions")

    def prepare(self, prediction, now=None):
        now = now or utcnow()
        self.expire(now)
        slot = next_slot(now)
        if not 0 < (slot-now).total_seconds() <= 60 or slot >= parse_time(self.state["ends_at"]):
            return
        existing = {r["id"] for r in self.state["records"]}
        block, index = divmod((int(slot.timestamp())-int(next_slot(parse_time(self.state["registered_at"])).timestamp()))//900, 8)
        deck = ["yes"]*4+["no"]*4
        random.Random(f"{self.state['seed']}:value:{block}").shuffle(deck)
        choices = {}
        if not any(self.risk(a, now)["open"] for a in ("balanced_value", "balanced_patient")):
            choices.update(balanced_value=deck[index], balanced_patient=deck[index])
        generated = parse_time(prediction.get("generated_at"))
        p = number(prediction.get("probability_up"), -1)
        valid = (prediction.get("available") is True and generated
                 and 0 <= (now-generated).total_seconds() <= 90 and 0 < p < 1 and p != .5
                 and prediction.get("direction") == ("up" if p > .5 else "down"))
        if valid and not self.risk("forecast_fade_value", now)["open"]:
            choices["forecast_fade_value"] = "no" if p > .5 else "yes"
        for arm, side in choices.items():
            identity = f"{arm}:{int(slot.timestamp())}"
            if identity not in existing:
                self.state["records"].append({"id": identity, "arm": arm, "status": "waiting", "side": side,
                    "start_at": slot.isoformat(), "close_at": (slot+timedelta(minutes=15)).isoformat(),
                    "picked_at": now.isoformat(), "model_snapshot": deepcopy(prediction) if arm == "forecast_fade_value" else {},
                    "forecast_probability_up": p if arm == "forecast_fade_value" else None})
        self.persist()

    def anchor(self, row):
        return next((r for r in self.state["records"] if r["arm"] == "balanced_value" and r["start_at"] == row["start_at"]), None)

    def deadline(self, row):
        end = min(parse_time(row["start_at"])+timedelta(seconds=300), parse_time(self.state["ends_at"]))
        anchor = self.anchor(row) if row["arm"] == "balanced_patient" else None
        if anchor and anchor.get("entered_at"):
            end = min(end, parse_time(anchor["entered_at"])+timedelta(seconds=60))
        return end

    def expire(self, now):
        for r in self.state["records"]:
            if r["status"] == "waiting" and now >= self.deadline(r):
                r.update(status="missed", reason=r.get("last_rejection") or "entry_deadline", completed_at=now.isoformat())

    def budget(self, arm, now):
        risk = self.risk(arm, now)
        return max(0., min(2.5 if risk["drawdown"] >= 100 else 5., risk["cash"],
                           50-risk["daily_gross_loss"], 250-risk["drawdown"])) if not risk["open"] else 0.

    def quote_reasons(self, quote, row, now, price):
        reasons = confirmation_reasons(quote, 2, now)
        if quote.get("ticker") != row["ticker"] or quote.get("side") != row["side"]:
            reasons.append("quote_identity_mismatch")
        if not POLICY["minimum_price_cents"]-1e-9 <= price <= 49:
            reasons.append("outside_value_band")
        if quote.get("book_consistent") is not True or not 0 <= number(quote.get("spread_cents"), 999) <= 2:
            reasons.append("book_quality")
        if not parse_time(row["start_at"]) <= now < self.deadline(row):
            reasons.append("entry_deadline")
        return reasons

    def enter(self, market, snapshot, confirm, schedule, now=None, clock=utcnow):
        now = now or clock()
        window = btc_window(market)
        if not window or not window[0] <= now < window[0]+timedelta(seconds=300):
            return
        for row in self.state["records"]:
            if row["status"] != "waiting" or row.get("ticker") != market["ticker"] or snapshot.get("side") != row["side"]:
                continue
            price = number(snapshot.get("best_entry_price_cents"), -1)
            reasons = self.quote_reasons(snapshot, row, now, price)
            budget = self.budget(row["arm"], now)
            if budget <= 0:
                reasons.append("risk_limit")
            if row["arm"] == "balanced_patient":
                anchor = self.anchor(row)
                if not anchor or anchor["status"] not in {"filled", "settled"}:
                    reasons.append("waiting_for_reference_fill")
                elif now <= parse_time(anchor["entered_at"]) or price > anchor["entry_price"]-1+1e-9:
                    reasons.append("waiting_for_one_cent_improvement")
            if reasons:
                row["last_rejection"] = reasons[0]
                self.reject(reasons[0])
                continue
            try:
                contracts = int(budget/(price/100))
                while contracts and order_cost(price, contracts, schedule)[0] > budget+1e-9:
                    contracts -= 1
            except ValueError as exc:
                row["last_rejection"] = str(exc)
                self.reject(str(exc))
                continue
            if not contracts:
                row["last_rejection"] = "unit_below_one_contract"
                continue
            checked = confirm(row["ticker"], row["side"], contracts)
            checked_at = clock()
            final = number(checked.get("full_size_entry_price_cents"), -1)
            reasons = self.quote_reasons(checked, row, checked_at, final)
            if checked_at < now:
                reasons.append("clock_moved_backwards")
            if number(checked.get("available_contracts")) < contracts:
                reasons.append("insufficient_depth")
            if final > price+1e-9:
                reasons.append("price_moved_no_fill")
            if reasons:
                row["last_rejection"] = reasons[0]
                self.reject(reasons[0])
                continue
            cost, fees = order_cost(final, contracts, schedule)
            if cost > min(budget, self.budget(row["arm"], checked_at))+1e-9:
                row["last_rejection"] = "confirmed_cost_above_unit"
                continue
            row.update(status="filled", entered_at=checked_at.isoformat(), contracts=contracts,
                       entry_price=final, cost=cost, fees=fees, american_odds=american_odds(final),
                       fee_schedule=deepcopy(schedule), target_risk=budget,
                       signal_evidence={"decision": deepcopy(snapshot), "confirmation": deepcopy(checked)},
                       execution="simulated_limit_fok_visible_depth", live_order=False)
            self.persist()

    def summary(self, now=None):
        now = now or utcnow()
        arms = []
        for arm, name in ARMS.items():
            rows = [r for r in self.state["records"] if r["arm"] == arm]
            trades = [r for r in rows if r["status"] == "settled"]
            risk = self.risk(arm, now)
            days = len({local_day(r["entered_at"]) for r in trades})
            active = next((r for r in rows if r["status"] == "filled"), None) or next((r for r in reversed(rows) if r["status"] == "waiting"), None)
            arms.append({"arm": arm, "name": name, **risk, "profit": round(risk["equity"]-5000, 6),
                         "stress_profit": round(sum(r["stress_profit"] for r in trades), 6),
                         "settled": len(trades), "wins": sum(r["profit"] > 0 for r in trades),
                         "entry_days": days, "missed": sum(r["status"] == "missed" for r in rows),
                         "review_checkpoint": len(trades) >= 200 and days >= 10,
                         "active": {k: active.get(k) for k in ("status", "side", "ticker", "start_at", "cost", "last_rejection")} if active else None})
        pairs = []
        for r in self.state["records"]:
            if r["arm"] != "balanced_patient" or r["status"] not in {"missed", "settled"}:
                continue
            base = self.anchor(r)
            if base["status"] in {"missed", "settled"}:
                pairs.append((r, base))
        return {"version": VERSION, "mode": "shadow_only", "automatic_promotion": False,
                "generated_at": local_time(now.isoformat()), "registered_at": local_time(self.state["registered_at"]),
                "ends_at": local_time(self.state["ends_at"]), "policy": POLICY, "arms": arms,
                "scan_count": self.state["scan_count"], "rejections": self.state["rejections"],
                "completed": now >= parse_time(self.state["ends_at"]) and not any(r["status"] == "filled" for r in self.state["records"]),
                "paired": {"completed_windows": len(pairs),
                           "patient_minus_reference_profit": round(sum(p.get("profit", 0)-b.get("profit", 0) for p, b in pairs), 6),
                           "patient_missed_reference_filled": sum(p["status"] == "missed" and b["status"] == "settled" for p, b in pairs)},
                "note": "Three independent $5,000 shadow portfolios. Missed paired entries count as zero. Review at 200 settled markets and 10 entry days; no automatic promotion. Bankroll does not create an edge."}
