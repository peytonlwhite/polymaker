"""Six sequential BTC 15m shadow portfolios. No order client or live writes.

Decisions are committed before the window. A read-only, independently fetched
production depth confirmation is required for every hypothetical FOK fill.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import secrets
from zoneinfo import ZoneInfo

from crypto_execution_safety import confirmation_reasons
from crypto_market_regime import atomic_write_json, number, parse_time
from crypto_pricing import kalshi_order_fee
from crypto_evidence import retain_records, flush_evidence_archives

VERSION = "btc-random-cycles-v1"
CHICAGO = ZoneInfo("America/Chicago")
ARMS = {
    "random_fixed": {"name": "Random / fixed", "direction": "random", "sizing": "fixed"},
    "prediction_fixed": {"name": "Prediction / fixed", "direction": "prediction", "sizing": "fixed"},
    "random_recovery": {"name": "Random / confidence + recovery", "direction": "random", "sizing": "recovery"},
    "prediction_recovery": {"name": "Prediction / confidence + recovery", "direction": "prediction", "sizing": "recovery"},
    "balanced_random": {"name": "Balanced random / fixed", "direction": "balanced", "sizing": "fixed"},
    "random_win_press": {"name": "Random / capped win press", "direction": "win_press", "sizing": "win_press"},
}
POLICY = {"version": VERSION, "bankroll": 5000., "base_unit": 5., "maximum_risk": 10.,
          "minimum_price_cents": 100/3, "maximum_price_cents": 60.,
          "preselect_seconds": 60, "entry_close_buffer_seconds": 5,
          "maximum_quote_age_seconds": 2, "maximum_spread_cents": 5,
          "gross_daily_loss_limit": 50., "drawdown_pause": 250., "drawdown_reduce": 100.,
          "recovery_cap": 1.25, "recovery_fraction": .02, "recovery_streak_limit": 6,
          "arms": ARMS, "automatic_promotion": False}
POLICY_HASH = hashlib.sha256(json.dumps(POLICY, sort_keys=True).encode()).hexdigest()


def utcnow():
    return datetime.now(timezone.utc)


def local_day(value):
    return parse_time(value).astimezone(CHICAGO).date().isoformat()


def local_time(value):
    return parse_time(value).astimezone(CHICAGO).isoformat() if value else None


def next_slot(now):
    return datetime.fromtimestamp((int(now.timestamp())//900+1)*900, timezone.utc)


def american_odds(price):
    return -100*price/(100-price) if price >= 50 else 100*(100-price)/price


def btc_window(market):
    """Exchange open_time is listing time, not the start of the 15m target."""
    ticker = str(market.get("ticker") or "")
    close = parse_time(market.get("close_time"))
    if not ticker.startswith("KXBTC15M-") or not close or close.timestamp() % 900:
        return None
    if market.get("series_ticker") not in (None, "", "KXBTC15M"):
        return None
    if market.get("market_type") not in (None, "binary") or market.get("strike_type") not in (None, "greater", "greater_or_equal"):
        return None
    if str(market.get("status") or "").lower() not in {"open", "active"}:
        return None
    return close-timedelta(minutes=15), close


def sizing(arm, risk, probability):
    base = POLICY["base_unit"]
    recovery = 0.
    if ARMS[arm]["sizing"] == "recovery":
        # Confidence is evidence about the randomly selected side, not a claim
        # that a fair random selector has predictive skill.
        multiplier = .5 + max(0., min(.75, (probability-.5)/.10))
        if probability >= .5 and risk["loss_streak"] < POLICY["recovery_streak_limit"]:
            recovery = min(POLICY["recovery_cap"], risk["drawdown"]*POLICY["recovery_fraction"])
        target = base * multiplier + recovery
    elif ARMS[arm]["sizing"] == "win_press":
        # Spend only a bounded share of the last two consecutive wins.
        target = base + min(2.5, risk["press_profit"]*.25)
    else:
        target = base
    if risk["drawdown"] >= POLICY["drawdown_reduce"]:
        target *= .5
        recovery = 0.
        target = min(target, base*.5)
    target = min(target, POLICY["maximum_risk"], risk["cash"],
                 max(0., POLICY["gross_daily_loss_limit"]-risk["daily_gross_loss"]),
                 max(0., POLICY["drawdown_pause"]-risk["drawdown"]))
    return {"target_risk": round(max(0., target), 6), "base_unit": base,
            "confidence": probability, "recovery_requested": recovery,
            "confidence_kind": "experimental_model_support" if ARMS[arm]["sizing"] == "recovery" else "fair_random" if ARMS[arm]["direction"] != "prediction" else "experimental_model_support"}


def order_cost(price, count, schedule):
    if schedule.get("stale") or schedule.get("series_ticker") != "KXBTC15M":
        raise ValueError("current_btc_fee_unavailable")
    fee = kalshi_order_fee(price, count, schedule=schedule)
    if not fee.get("authoritative") or not fee.get("exact"):
        raise ValueError("authoritative_fee_unavailable")
    # Whole-dollar-contract count; all units risk principal plus conservative
    # member cash rounding. Actual member fill rebates are not assumed.
    cost = math.ceil((price*count/100+fee["conservative_fee_dollars"]-1e-9)*100)/100
    return cost, round(cost-price*count/100, 6)


class RandomCycleLab:
    def __init__(self, path, now=None):
        self.path = Path(path)
        now = now or utcnow()
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if self.state.get("policy_hash") != POLICY_HASH or self.state.get("version") != VERSION:
                raise ValueError("Shadow policy changed: preserve this ledger and use a new cohort")
            self.audit()
        else:
            self.state = {"version": VERSION, "policy_hash": POLICY_HASH, "policy": deepcopy(POLICY),
                          "registered_at": now.isoformat(), "seed": secrets.token_hex(32),
                          "records": [], "forecasts": [], "rejections": {}, "scan_count": 0}
            self.persist()

    def audit(self):
        if not isinstance(self.state.get("seed"), str) or not self.state["seed"]:
            raise ValueError("Missing persisted random seed")
        ids = set()
        open_counts = {a: 0 for a in ARMS}
        for row in self.state["records"]:
            if row["id"] in ids or row["arm"] not in ARMS:
                raise ValueError("Duplicate cycle or unknown arm")
            ids.add(row["id"])
            if row["status"] not in {"waiting", "missed", "filled", "settled"} or row["side"] not in {"yes", "no"}:
                raise ValueError("Invalid persisted cycle")
            if parse_time(row["picked_at"]) >= parse_time(row["start_at"]):
                raise ValueError("Post-start direction in persisted ledger")
            if row["status"] in {"filled", "settled"}:
                if not 0 < row["cost"] <= POLICY["maximum_risk"] + 1e-6:
                    raise ValueError("Invalid shadow exposure")
                if (not isinstance(row["contracts"], int) or row["contracts"] < 1
                        or not POLICY["minimum_price_cents"]-1e-9 <= row["entry_price"] <= POLICY["maximum_price_cents"]
                        or not math.isfinite(row["fees"]) or row["fees"] < 0
                        or abs(row["entry_price"]*row["contracts"]/100+row["fees"]-row["cost"]) > 1e-6):
                    raise ValueError("Invalid shadow fill accounting")
                if row["status"] == "filled":
                    open_counts[row["arm"]] += 1
                elif abs(row["profit"] - (row["payout"]-row["cost"])) > 1e-6:
                    raise ValueError("Shadow settlement accounting mismatch")
                elif not math.isfinite(row["profit"]) or row["payout"] not in (0, row["contracts"]):
                    raise ValueError("Invalid shadow payout")
        if any(v > 1 for v in open_counts.values()):
            raise ValueError("Overlapping shadow positions")

    def persist(self):
        self.audit()
        retain_records(self.state, 600)
        flush_evidence_archives(self.path, self.state)
        atomic_write_json(self.path, self.state)

    def reject(self, reason):
        counts = self.state["rejections"]
        counts[reason] = counts.get(reason, 0)+1

    def risk(self, arm, now):
        rows = [r for r in self.state["records"] if r["arm"] == arm]
        settled = sorted([r for r in rows if r["status"] == "settled"], key=lambda r: r["settled_at"])
        opened = [r for r in rows if r["status"] == "filled"]
        equity = peak = POLICY["bankroll"]
        maximum_dd = 0.
        for row in settled:
            equity += row["profit"]
            peak = max(peak, equity)
            maximum_dd = max(maximum_dd, peak-equity)
        loss_streak = 0
        for row in reversed(settled):
            if row["profit"] >= 0:
                break
            loss_streak += 1
        press_profit = 0.
        for row in settled[-2:][::-1]:
            if row["profit"] <= 0:
                break
            press_profit += row["profit"]
        gross = sum(max(0., -r["profit"]) for r in settled
                    if local_day(r["settled_at"]) == now.astimezone(CHICAGO).date().isoformat())
        exposure = sum(r["cost"] for r in opened)
        return {"equity": round(equity, 6), "cash": round(equity-exposure, 6),
                "exposure": round(exposure, 6), "drawdown": round(peak-equity, 6),
                "max_drawdown": round(maximum_dd, 6), "loss_streak": loss_streak,
                "press_profit": press_profit, "daily_gross_loss": round(gross, 6),
                "open": len(opened)}

    def random_side(self, family, slot):
        # A random seed chosen once, then a keyed digest: restart-stable, paired
        # controls, with no dependence on prices, model score or outcomes.
        message = f"{self.state['seed']}:{family}:{int(slot.timestamp())}".encode()
        return "yes" if hashlib.sha256(message).digest()[0] & 1 else "no"

    def balanced_side(self):
        prior = sum(r["arm"] == "balanced_random" for r in self.state["records"])
        block, index = divmod(prior, 8)
        rng = random.Random(f"{self.state['seed']}:balanced:{block}")
        deck = ["yes"]*4 + ["no"]*4
        rng.shuffle(deck)
        return deck[index]

    def expire(self, now):
        for row in self.state["records"]:
            if row["status"] == "waiting" and now >= parse_time(row["close_at"]):
                row.update(status="missed", reason=row.get("last_rejection") or "no_eligible_entry",
                           completed_at=now.isoformat())
        for row in self.state["forecasts"]:
            if (row["horizon"] == "15m" and row["status"] == "open" and not row.get("ticker")
                    and now >= parse_time(row["due_at"])+timedelta(minutes=5)):
                row.update(status="expired", reason="market_not_discovered", resolved_at=now.isoformat())

    def prepare(self, prediction, now=None):
        now = now or utcnow()
        self.expire(now)
        slot = next_slot(now)
        if not 0 < (slot-now).total_seconds() <= POLICY["preselect_seconds"]:
            return
        predicted_at = parse_time(prediction.get("generated_at"))
        valid = bool(prediction.get("available") and predicted_at and
                     0 <= (now-predicted_at).total_seconds() <= 90)
        p_up = number(prediction.get("probability_up"), .5) if valid else .5
        existing = {r["id"] for r in self.state["records"]}
        for arm, spec in ARMS.items():
            identity = f"{arm}:{int(slot.timestamp())}"
            if identity in existing or self.risk(arm, now)["open"]:
                continue
            if spec["direction"] == "prediction":
                if not valid or prediction.get("direction") not in {"up", "down"}:
                    self.reject("prediction_unavailable_before_start")
                    continue
                side = "yes" if prediction["direction"] == "up" else "no"
            elif spec["direction"] == "balanced":
                side = self.balanced_side()
            else:
                side = self.random_side(spec["direction"], slot)
            row = {"id": identity, "arm": arm, "status": "waiting", "side": side,
                   "start_at": slot.isoformat(), "close_at": (slot+timedelta(minutes=15)).isoformat(),
                   "picked_at": now.isoformat(), "probability_up": p_up,
                   "side_confidence": p_up if side == "yes" else 1-p_up,
                   "model_available": valid, "model_snapshot": deepcopy(prediction) if valid else {},
                   "random_selector_probability": .5 if spec["direction"] != "prediction" else None}
            self.state["records"].append(row)
        # Score the 15m forecast even if entry never occurs: no fill-selection bias.
        fid = f"BTC:15m:{int(slot.timestamp())}"
        if valid and not any(r["id"] == fid for r in self.state["forecasts"]):
            self.state["forecasts"].append({"id": fid, "asset": "BTC", "horizon": "15m",
                "status": "open", "captured_at": now.isoformat(), "start_at": slot.isoformat(),
                "due_at": (slot+timedelta(minutes=15)).isoformat(),
                "raw_probability_up": prediction["raw_probability_up"], "probability_up": p_up})
        # Commit the draw before any market discovery or entry call.
        self.persist()

    def bind_markets(self, markets):
        windows = {}
        for market in markets:
            window = btc_window(market)
            if window:
                windows.setdefault(window[0].isoformat(), []).append(market)
        for row in self.state["records"]:
            choices = windows.get(row["start_at"], [])
            if row["status"] == "waiting" and len(choices) == 1:
                ticker = choices[0]["ticker"]
                if row.get("ticker") not in (None, ticker):
                    raise ValueError("Market binding changed")
                row["ticker"] = ticker
        for row in self.state["forecasts"]:
            choices = windows.get(row.get("start_at"), [])
            if row["horizon"] == "15m" and row["status"] == "open" and len(choices) == 1:
                row["ticker"] = choices[0]["ticker"]

    def enter(self, market, snapshot, confirm, schedule, now=None, clock=utcnow):
        now = now or clock()
        window = btc_window(market)
        if not window or not window[0] <= now < window[1]-timedelta(seconds=POLICY["entry_close_buffer_seconds"]):
            return
        for row in self.state["records"]:
            if row["status"] != "waiting" or row.get("ticker") != market["ticker"]:
                continue
            if snapshot.get("side") != row["side"]:
                continue
            arm = row["arm"]
            risk = self.risk(arm, now)
            reasons = confirmation_reasons(snapshot, POLICY["maximum_quote_age_seconds"], now)
            if risk["open"]:
                reasons.append("awaiting_settlement")
            if snapshot.get("ticker") != row["ticker"] or snapshot.get("side") != row["side"]:
                reasons.append("quote_identity_mismatch")
            price = number(snapshot.get("best_entry_price_cents"), -1)
            if not POLICY["minimum_price_cents"]-1e-9 <= price <= POLICY["maximum_price_cents"]:
                reasons.append("outside_odds_band")
            if snapshot.get("book_consistent") is not True or not 0 <= number(snapshot.get("spread_cents"), 999) <= POLICY["maximum_spread_cents"]:
                reasons.append("book_quality")
            plan = sizing(arm, risk, row["side_confidence"])
            if plan["target_risk"] <= 0:
                reasons.append("risk_limit")
            if reasons:
                row["last_rejection"] = reasons[0]
                self.reject(reasons[0])
                continue
            try:
                contracts = int(plan["target_risk"]/(price/100))
                while contracts > 0 and order_cost(price, contracts, schedule)[0] > plan["target_risk"]+1e-9:
                    contracts -= 1
            except ValueError as exc:
                row["last_rejection"] = str(exc)
                self.reject(str(exc))
                continue
            if contracts < 1:
                row["last_rejection"] = "unit_below_one_contract"
                continue
            checked = confirm(row["ticker"], row["side"], contracts)
            checked_at = clock()
            reasons = confirmation_reasons(checked, POLICY["maximum_quote_age_seconds"], checked_at)
            final_price = number(checked.get("full_size_entry_price_cents"), -1)
            if checked.get("ticker") != row["ticker"] or checked.get("side") != row["side"]:
                reasons.append("confirmation_identity_mismatch")
            if number(checked.get("available_contracts")) < contracts or final_price < 0:
                reasons.append("insufficient_depth")
            if checked.get("book_consistent") is not True or not 0 <= number(checked.get("spread_cents"), 999) <= POLICY["maximum_spread_cents"]:
                reasons.append("confirmation_book_quality")
            # Model a limit FOK order at the originally observed executable ask.
            if final_price > price+1e-9:
                reasons.append("price_moved_no_fill")
            if not POLICY["minimum_price_cents"]-1e-9 <= final_price <= POLICY["maximum_price_cents"]:
                reasons.append("confirmation_outside_odds_band")
            if checked_at >= window[1]-timedelta(seconds=POLICY["entry_close_buffer_seconds"]):
                reasons.append("confirmation_after_entry_cutoff")
            if reasons:
                row["last_rejection"] = reasons[0]
                self.reject(reasons[0])
                continue
            # Charge every contract at the worst executable level; conservative
            # versus price improvement across the actual depth ladder.
            cost, fees = order_cost(final_price, contracts, schedule)
            if cost > plan["target_risk"]+1e-9:
                row["last_rejection"] = "confirmed_cost_above_unit"
                continue
            row.update(status="filled", entered_at=checked_at.isoformat(), contracts=contracts,
                       entry_price=final_price, american_odds=american_odds(final_price),
                       cost=cost, fees=fees, sizing=plan, fee_schedule=deepcopy(schedule),
                       signal_evidence={"decision": deepcopy(snapshot), "confirmation": deepcopy(checked)},
                       execution="simulated_limit_fok_visible_depth", live_order=False)
            for future in self.state["records"]:
                if future["arm"] == arm and future["status"] == "waiting" and parse_time(future["start_at"]) > window[0]:
                    future.update(status="missed", reason="prior_position_open", completed_at=checked_at.isoformat())
            self.persist()

    def settle(self, fetch_market, now=None):
        now = now or utcnow()
        pending = [r for r in self.state["records"] if r["status"] == "filled" and parse_time(r["close_at"]) <= now]
        pending += [r for r in self.state["forecasts"] if r["horizon"] == "15m" and r["status"] == "open"
                    and r.get("ticker") and parse_time(r["due_at"]) <= now]
        for ticker in sorted({r["ticker"] for r in pending}):
            try:
                market = fetch_market(ticker)
            except Exception:
                self.reject("settlement_fetch_failed")
                continue
            if (market.get("ticker") != ticker
                    or str(market.get("status") or "").lower() not in {"finalized", "settled"}
                    or market.get("is_provisional") is True):
                continue
            # Require official binary result; neither last traded price nor
            # spot direction is a settlement. Ambiguous/void outcomes stay held.
            outcome = str(market.get("result") or "").lower()
            if outcome not in {"yes", "no"}:
                self.reject("settlement_result_unavailable")
                continue
            cash_result = market.get("settlement_value_dollars")
            if cash_result not in (None, "") and number(cash_result, -1) != int(outcome == "yes"):
                self.reject("settlement_cash_result_ambiguous")
                continue
            for row in pending:
                if row["ticker"] != ticker:
                    continue
                if row.get("horizon") == "15m":
                    row.update(status="resolved", outcome=int(outcome == "yes"), resolved_at=now.isoformat())
                else:
                    payout = row["contracts"] if outcome == row["side"] else 0.
                    row.update(status="settled", settled_at=now.isoformat(), result=outcome,
                               payout=payout, profit=round(payout-row["cost"], 6),
                               stress_profit=round(payout-row["cost"]-.01*row["contracts"], 6))
        self.expire(now)
        self.persist()

    def summary(self, now=None):
        now = now or utcnow()
        arms = []
        for arm, spec in ARMS.items():
            rows = [r for r in self.state["records"] if r["arm"] == arm]
            trades = [r for r in rows if r["status"] == "settled"]
            risk = self.risk(arm, now)
            active = next((r for r in rows if r["status"] == "filled"), None)
            active = active or next((r for r in reversed(rows) if r["status"] == "waiting"), None)
            last = rows[-1] if rows else {}
            cost = sum(r["cost"] for r in trades)
            arms.append({"arm": arm, "name": spec["name"], **risk,
                         "bankroll": POLICY["bankroll"], "profit": risk["equity"]-POLICY["bankroll"],
                         "fees": sum(r["fees"] for r in trades), "settled": len(trades),
                         "wins": sum(r["profit"] > 0 for r in trades), "opportunities": len(rows),
                         "missed": sum(r["status"] == "missed" for r in rows),
                         "stress_profit": sum(r["stress_profit"] for r in trades),
                         "roi": sum(r["profit"] for r in trades)/cost if cost else None,
                         "active": {k: active.get(k) for k in ("status", "side", "ticker", "side_confidence", "model_available", "start_at", "close_at", "picked_at", "entered_at", "cost", "contracts", "last_rejection")} if active else None,
                         "last_reason": last.get("last_rejection") or last.get("reason"),
                         "paused": risk["drawdown"] >= POLICY["drawdown_pause"] or risk["daily_gross_loss"] >= POLICY["gross_daily_loss_limit"]})
        return {"version": VERSION, "mode": "shadow_only", "automatic_promotion": False,
                "generated_at": now.astimezone(CHICAGO).isoformat(),
                "registered_at": local_time(self.state["registered_at"]), "policy": POLICY,
                "arms": arms, "scan_count": self.state["scan_count"],
                "rejections": self.state["rejections"], "forecast": self.state.get("latest_forecast", {}),
                "recent": [{**{k: r.get(k) for k in ("arm", "status", "side", "ticker", "cost", "contracts", "american_odds", "profit", "reason")},
                            "start_at": local_time(r["start_at"]), "picked_at": local_time(r["picked_at"])}
                           for r in self.state["records"][-30:][::-1]],
                "note": "Six independent $5,000 simulations. Results overlap; do not sum them. No edge gate. Bankroll and sizing do not create positive expectancy."}
