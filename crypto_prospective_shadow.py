"""Preregistered, prospective Crypto research. No order or account API exists here.

Read-only quote callbacks are injected by the scanner. Controls are diagnostic;
only one primary per ticker can consume the common virtual portfolio. Outcomes
and fixed reviews are immutable. Missing inputs fail closed and remain counted.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

from crypto_execution_safety import confirmation_reasons
from crypto_evidence import flush_evidence_archives, retain_records
from crypto_pricing import kalshi_order_fee

VERSION = "crypto-prospective-2026-09-v1"
PRIMARY = ("eth_flow", "eth_persistence", "spot_no_chase", "distance_favorite", "brti_value")
PROTOCOL = {
    "version": VERSION, "primary_lanes": PRIMARY, "shadow_only": True,
    "automatic_promotion": False, "historical_backfill": False,
    "eth": {"price": [35, 65], "minutes": [2, 13], "flow_each": .15,
            "flow_mean": .30, "trades_each": 20, "persistence_300s_each": .10,
            "assignment": "sha256(ticker) even=flow odd=persistence"},
    "spot": {"assets": ["BTC", "ETH", "SOL", "DOGE", "XRP"], "price": [35, 80],
             "minutes": [5, 13], "return_each_bps": 5, "return_mean_bps": 10,
             "signed_kalshi_move_max_cents": .75, "adverse_cents": 0, "control_adverse_cents": 1},
    "favorite": {"price": [55, 85], "minutes": [2, 6], "abs_sigma": 1.25,
                 "minimum_lower_edge_cents": 2, "requires_empirical_interval": True},
    "brti": {"readings": [35, 45], "price": [50, 95], "contracts": 1,
             "depth_control_contracts": 10, "minimum_lower_edge_cents": 2,
             "probability": "preregistered robust tick mixture; no legacy residual fitting"},
    "execution": {"contracts": 3, "source_age_seconds": 2, "book_age_seconds": 1,
                  "quality": .9, "spread_cents": 2, "independent_rest_confirmation": True,
                  "fee": "conservative_cent_cash_alignment", "stress_cents_per_contract": 1},
    "portfolio": {"daily_entries": 25, "open": 4, "per_expiry": 2,
                  "exposure_dollars": 45, "daily_loss_dollars": 45,
                  "priority": list(PRIMARY), "same_ticker_once": True, "recovery": False},
    "review": {"interval_days": 30, "family_size": 5, "alpha": .05,
               "minimum_markets": {"eth_flow": 200, "eth_persistence": 200,
                   "spot_no_chase": 100, "distance_favorite": 200, "brti_value": 50},
               "minimum_expiries": {"eth_flow": 100, "eth_persistence": 100,
                   "spot_no_chase": 50, "distance_favorite": 100, "brti_value": 50},
               "minimum_active_dates": {"eth_flow": 20, "eth_persistence": 20,
                   "spot_no_chase": 20, "distance_favorite": 20, "brti_value": 14},
               "method": "weighted bounded cluster Hoeffding; day and expiry; alpha spending",
               "assumption": "independence between clusters; dependence within clusters unrestricted"},
}
# Hash the implementation too: edits register a new forward cohort on restart.
CODE_HASH = hashlib.sha256(b"".join((Path(__file__).parent / name).read_bytes() for name in (
    "crypto_prospective_shadow.py", "crypto_pricing.py", "crypto_execution_safety.py",
    "crypto_paper_bettor.py", "crypto_settlement_lag_shadow.py", "crypto_market_stream.py",
    "crypto_microstructure.py", "crypto_15m_learning.py"))).hexdigest()
POLICY_HASH = hashlib.sha256(json.dumps({"protocol": PROTOCOL, "code": CODE_HASH},
                                      sort_keys=True).encode()).hexdigest()
CHICAGO = ZoneInfo("America/Chicago")


def stamp(value=None):
    return value or datetime.now(timezone.utc)


def parsed(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def local_day(value):
    dt = parsed(value)
    return dt.astimezone(CHICAGO).date().isoformat() if dt else "unknown"


def empty_ledger(now=None):
    return {"version": VERSION, "registered_at": stamp(now).isoformat(),
            "policy_hash": POLICY_HASH, "protocol": deepcopy(PROTOCOL), "code_hash": CODE_HASH,
            "records": [], "attempts": [], "forecasts": [], "reviews": [], "policy_history": [],
            "rejection_counts": {}, "screening_counts": {}, "scan_count": 0, "mode": "shadow_only",
            "affects_execution": False, "automatic_promotion": False}


def normalize(ledger, now=None):
    if not ledger:
        return empty_ledger(now)
    if ledger.get("policy_hash") != POLICY_HASH:
        ledger.setdefault("policy_history", []).append({
            "policy_hash": ledger.get("policy_hash"), "registered_at": ledger.get("registered_at"),
            "protocol": ledger.get("protocol"), "ended_at": stamp(now).isoformat()})
        ledger.update({k: v for k, v in empty_ledger(now).items()
                       if k not in {"records", "attempts", "forecasts", "reviews", "policy_history", "rejection_counts", "screening_counts", "scan_count"}})
    ledger.setdefault("forecasts", [])
    ledger.setdefault("screening_counts", {})
    return ledger


def quality_reasons(candidate, now):
    reasons = []
    close = parsed(candidate.get("close_time"))
    if not close or now >= close:
        reasons.append("market_closed_or_time_missing")
    if number((candidate.get("data_quality") or {}).get("score")) < .9:
        reasons.append("quality_below_0.90")
    book = candidate.get("kalshi_microstructure") or {}
    if (not book.get("fresh") or book.get("sequence_valid") is not True
            or not book.get("book_consistent") or not 0 <= number(book.get("age_seconds"), 999) <= 1):
        reasons.append("book_stale_or_invalid")
    if not 0 <= number(book.get("spread_yes_cents"), 999) <= 2:
        reasons.append("spread_over_2c")
    for name in ("coinbase", "kraken"):
        venue = (candidate.get("microstructure") or {}).get(name) or {}
        if (not venue.get("connected") or not 0 <= number(venue.get("stream_age_seconds"), 999) <= 2
                or not 0 <= number(venue.get("book_age_seconds"), 999) <= 2):
            reasons.append(name + "_stale_or_missing")
    return reasons


def hypothesis(candidate, lane, now):
    """Return a side and bounded entry rule using only this decision snapshot."""
    minutes = ((parsed(candidate.get("close_time")) or now) - now).total_seconds() / 60
    venues = [(candidate.get("microstructure") or {}).get(v) or {} for v in ("coinbase", "kraken")]
    kind = candidate.get("market_kind")
    if kind not in {"above", "below"}:
        return None
    orientation = -1 if kind == "below" else 1
    if lane.startswith("eth_"):
        if candidate.get("asset") != "ETH" or not 2 <= minutes <= 13:
            return None
        values = [number(v.get("trade_flow_60s")) for v in venues]
        direction = 1 if sum(values) > 0 else -1
        if (min(v * direction for v in values) < .15 or abs(sum(values) / 2) < .30
                or min(number(v.get("trade_count_60s")) for v in venues) < 20):
            return None
        if lane == "eth_persistence" and min(number(v.get("trade_flow_300s")) * direction for v in venues) < .10:
            return None
        return {"side": "yes" if direction * orientation > 0 else "no", "band": [35, 65], "contracts": 3}
    if lane.startswith("spot_"):
        if candidate.get("asset") not in PROTOCOL["spot"]["assets"] or not 5 <= minutes <= 13:
            return None
        values = [number(v.get("mid_return_60s_bps")) for v in venues]
        direction = 1 if sum(values) > 0 else -1
        movement = (candidate.get("kalshi_microstructure") or {}).get("yes_mid_change_60s_pp")
        if (min(v * direction for v in values) < 5 or abs(sum(values) / 2) < 10 or movement is None
                or number(movement) * direction * orientation > .75):
            return None
        return {"side": "yes" if direction * orientation > 0 else "no", "band": [35, 80], "contracts": 3}
    if lane.startswith("distance_"):
        if not 2 <= minutes <= 6:
            return None
        yes, no = number(candidate.get("yes_ask")), number(candidate.get("no_ask"))
        side = "yes" if yes >= no else "no"
        return {"side": side, "band": [55, 85], "contracts": 3}
    return None


def favorite_reasons(candidate, side, price, fee, contracts):
    interval = candidate.get("probability_interval") or {}
    probability = candidate.get("probability") or {}
    result = []
    if abs(number(probability.get("target_distance_sigma"))) < 1.25:
        result.append("distance_below_1.25_sigma")
    if interval.get("interval_status") != "empirical_coverage_qualified":
        result.append("empirical_interval_unqualified")
    low = number(interval.get("p_low")) if side == "yes" else 100 - number(interval.get("p_high"), 100)
    if low - price - fee / contracts * 100 - 1 < 2:
        result.append("lower_edge_below_2c_after_stress")
    return result


def portfolio_reasons(ledger, row, now):
    # Limits span policy versions and include outstanding virtual positions.
    allocated = [r for r in ledger["records"] if r.get("portfolio_included")]
    opened = [r for r in allocated if r.get("status") == "open"]
    day = now.astimezone(CHICAGO).date().isoformat()
    reasons = []
    if any(r.get("ticker") == row["ticker"] for r in allocated):
        reasons.append("portfolio_ticker_already_allocated")
    if len([r for r in allocated if local_day(r.get("captured_at")) == day]) >= 25:
        reasons.append("portfolio_daily_entry_limit")
    if len(opened) >= 4:
        reasons.append("portfolio_open_limit")
    if sum(r.get("expiry_key") == row["expiry_key"] for r in opened) >= 2:
        reasons.append("portfolio_expiry_limit")
    if sum(number(r.get("cost_dollars")) for r in opened) + row["cost_dollars"] > 45:
        reasons.append("portfolio_exposure_limit")
    losses = sum(min(0, number(r.get("profit_dollars"))) for r in allocated
                 if local_day(r.get("settled_at")) == day)
    if losses <= -45:
        reasons.append("portfolio_daily_loss_limit")
    return reasons


def record_attempt(ledger, row, reasons):
    row = {**row, "status": "rejected" if reasons else "accepted", "reasons": list(dict.fromkeys(reasons))}
    ledger["attempts"].append(row)
    for reason in set(reasons):
        key = row["lane"] + ":" + reason
        ledger["rejection_counts"][key] = ledger["rejection_counts"].get(key, 0) + 1
    return row


def capture(ledger, candidates, refresh, confirm, now=None):
    current = stamp(now)
    known = {r["id"] for r in ledger["records"]}
    added = []
    ledger["scan_count"] += 1
    for candidate in sorted(candidates or [], key=lambda c: str(c.get("ticker"))):
        current = stamp(now)
        if candidate.get("market_lane") != "crypto_15m":
            continue
        initial = refresh(candidate)
        for lane in (*PRIMARY[:4], "spot_one_cent_control", "distance_price_time_control"):
            identity = f"{POLICY_HASH}:{lane}:{candidate.get('ticker')}"
            if identity in known:
                continue
            rule = hypothesis(initial, lane, current)
            if not rule:
                reason = lane + ":" + (initial.get("error") or "signal_or_time_not_matched")
                ledger["screening_counts"][reason] = ledger["screening_counts"].get(reason, 0) + 1
                continue
            side, contracts = rule["side"], rule["contracts"]
            signal = number(initial.get(side + "_ask"))
            if not rule["band"][0] <= signal <= rule["band"][1]:
                continue
            reasons = quality_reasons(initial, current)
            result, latest = {}, initial
            if not reasons:
                # Production adapter forces a new REST read, then refreshes venue inputs.
                result = confirm(candidate["ticker"], side, contracts)
                latest = refresh(candidate)
            checked = stamp(now)
            latest_rule = hypothesis(latest, lane, checked)
            reasons += quality_reasons(latest, checked)
            if not latest_rule or latest_rule["side"] != side:
                reasons.append("signal_changed_on_confirmation")
            reasons += confirmation_reasons(result, 1, checked)
            price = number(result.get("full_size_entry_price_cents"))
            if number(result.get("available_contracts")) < contracts or price <= 0:
                reasons.append("insufficient_full_size_depth")
            if not rule["band"][0] <= price <= rule["band"][1]:
                reasons.append("confirmed_price_outside_band")
            allowance = 1 if lane == "spot_one_cent_control" else 0
            if price - signal > allowance + 1e-9:
                reasons.append("adverse_confirmation_move")
            fee = kalshi_order_fee(price, contracts, schedule=initial.get("fee_schedule") or {})
            if not fee.get("authoritative") or not fee.get("exact"):
                reasons.append("supported_authoritative_fee_missing")
            fee_dollars = number(fee.get("conservative_fee_dollars"))
            if lane == "distance_favorite":
                reasons += favorite_reasons(latest, side, price, fee_dollars, contracts)
            if checked < (parsed(ledger["registered_at"]) or checked):
                reasons.append("before_registration")
            row = {"id": identity, "policy_hash": POLICY_HASH, "lane": lane,
                   "ticker": candidate.get("ticker"), "asset": candidate.get("asset"),
                   "side": side, "captured_at": checked.isoformat(), "close_time": candidate.get("close_time"),
                   "expiry_key": candidate.get("close_time"), "contracts": contracts,
                   "signal_price_cents": signal, "entry_price_cents": price,
                   "fee_dollars": fee_dollars, "cost_dollars": price / 100 * contracts + fee_dollars,
                   "fee_assumption": "conservative_cent_cash_alignment; net_fee_unverified",
                   "confirmation": result, "fresh_input_snapshot": deepcopy(latest),
                   "minutes_to_close": ((parsed(candidate.get("close_time")) or checked) - checked).total_seconds() / 60,
                   "probability_yes": (latest.get("probability_interval") or {}).get("p_yes"),
                   "market_probability_yes": (latest.get("probability") or {}).get("market_implied_yes"),
                   "status": "open", "portfolio_included": False, "automatic_promotion": False}
            record_attempt(ledger, deepcopy(row), reasons)
            if reasons:
                continue
            # ETH arms are assigned before outcomes; the unassigned arm is a control.
            assigned = "eth_flow" if int(hashlib.sha256(str(row["ticker"]).encode()).hexdigest(), 16) % 2 == 0 else "eth_persistence"
            row["primary_sample"] = lane in PRIMARY and (not lane.startswith("eth_") or lane == assigned)
            row["portfolio_reasons"] = portfolio_reasons(ledger, row, checked) if row["primary_sample"] else ["diagnostic_control"]
            row["portfolio_included"] = row["primary_sample"] and not row["portfolio_reasons"]
            ledger["records"].append(row)
            added.append(row)
            known.add(identity)
    return added


def capture_brti(ledger, market, review, cf, book, history, confirm, now=None):
    """Capture fixed readings directly in the official feed worker, before close."""
    from crypto_settlement_lag_shadow import settlement_probability, _robust_tick_sigma
    current = stamp(now)
    reading = int(number(cf.get("window_size")))
    if reading not in (35, 45):
        return
    ticker = market.get("ticker")
    identity = f"{POLICY_HASH}:brti_observation:{ticker}:{reading}"
    if any(r["id"] == identity for r in ledger["forecasts"]):
        return
    close = parsed(market.get("close_time"))
    source = number(cf.get("source_ts_ms")) / 1000
    reasons = []
    if (not close or current >= close or not 0 <= current.timestamp() - source <= 2
            or source >= close.timestamp() or current < parsed(ledger["registered_at"])):
        reasons.append("late_or_unverifiable_source_arrival")
    if not all(review.get(k) is True for k in ("source_time_aligned", "sequence_valid", "window_integrity")):
        reasons.append("official_window_invalid")
    if reasons:
        record_attempt(ledger, {"id": identity, "lane": "brti_value", "ticker": ticker,
                               "captured_at": current.isoformat(), "reading": reading}, reasons)
        return
    sigma, _, _ = _robust_tick_sigma(history, number(cf.get("value")))
    probability = settlement_probability(target=number(market.get("target")),
        observed_average=number(cf.get("window_average")), observed_count=reading,
        current_value=number(cf.get("value")), tick_sigma=sigma, calibration_samples={},
        market_kind=market.get("market_kind") or "above", probability_cap_pct=98,
        minimum_empirical_samples=30)
    yes_bid = max((number(p) for p, *_ in book.get("yes_levels", [])), default=0)
    no_bid = max((number(p) for p, *_ in book.get("no_levels", [])), default=0)
    market_p = (yes_bid + 100 - no_bid) / 2 if yes_bid and no_bid else None
    spot = number(cf.get("value")) > number(market.get("target"))
    if market.get("market_kind") == "below":
        spot = not spot
    forecast = {"id": identity, "policy_hash": POLICY_HASH, "ticker": ticker, "reading": reading,
                "captured_at": current.isoformat(), "source_ts_ms": cf.get("source_ts_ms"),
                "close_time": market.get("close_time"), "status": "open", "probability": probability,
                "market_probability_yes": market_p, "spot_probability_yes": 100 if spot else 0,
                "observed_average": cf.get("window_average"), "target": market.get("target"),
                "required_remaining_average": (60 * number(market.get("target")) - reading * number(cf.get("window_average"))) / (60 - reading)}
    ledger["forecasts"].append(forecast)
    if (not book.get("fresh") or book.get("sequence_valid") is not True
            or not 0 <= number(book.get("age_seconds"), 999) <= 1
            or not yes_bid or not no_bid or not 0 <= 100 - yes_bid - no_bid <= 2):
        reasons.append("brti_book_stale_or_wide")
    side = "yes" if number(probability.get("p_yes")) >= (market_p if market_p is not None else 50) else "no"
    signal = 100 - (no_bid if side == "yes" else yes_bid)
    for lane, contracts in (("brti_value", 1), ("brti_depth_control", 10)):
        key = f"{POLICY_HASH}:{lane}:{ticker}"
        if any(r["id"] == key for r in ledger["records"]):
            continue
        rejected = list(reasons)
        quote = confirm(ticker, side, contracts) if not rejected else {}
        checked = stamp(now)
        rejected += confirmation_reasons(quote, 1, checked)
        if checked >= close or checked.timestamp() - source > 2:
            rejected.append("brti_confirmation_after_decision_window")
        price = number(quote.get("full_size_entry_price_cents"))
        if not 50 <= price <= 95 or price > signal:
            rejected.append("brti_price_band_or_chase")
        if number(quote.get("available_contracts")) < contracts or price <= 0:
            rejected.append("insufficient_full_size_depth")
        fee = kalshi_order_fee(price, contracts, schedule=market.get("fee_schedule") or {})
        if not fee.get("exact"):
            rejected.append("supported_authoritative_fee_missing")
        cost = number(fee.get("conservative_fee_dollars"))
        low = number(probability.get("p_low")) if side == "yes" else 100 - number(probability.get("p_high"), 100)
        if low - price - cost / contracts * 100 - 1 < 2:
            rejected.append("lower_edge_below_2c_after_stress")
        row = {"id": key, "policy_hash": POLICY_HASH, "lane": lane, "ticker": ticker, "asset": "BTC",
               "side": side, "captured_at": checked.isoformat(), "close_time": market.get("close_time"),
               "expiry_key": market.get("close_time"), "reading": reading, "contracts": contracts,
               "entry_price_cents": price, "signal_price_cents": signal, "fee_dollars": cost,
               "cost_dollars": price / 100 * contracts + cost, "confirmation": quote,
               "probability_yes": probability.get("p_yes"), "market_probability_yes": market_p,
               "fee_assumption": "conservative_cent_cash_alignment; net_fee_unverified",
               "fresh_input_snapshot": {"official": forecast, "book": book},
               "primary_sample": lane == "brti_value", "status": "open", "automatic_promotion": False}
        record_attempt(ledger, {k: v for k, v in row.items() if k != "fresh_input_snapshot"}, rejected)
        if rejected:
            continue
        row["portfolio_reasons"] = portfolio_reasons(ledger, row, checked) if row["primary_sample"] else ["diagnostic_control"]
        row["portfolio_included"] = row["primary_sample"] and not row["portfolio_reasons"]
        ledger["records"].append(row)


def settle(ledger, fetch_market, now=None):
    current = stamp(now)
    outcomes = {}
    for row in ledger["records"] + ledger.get("forecasts", []):
        close = parsed(row.get("close_time"))
        if row.get("status") != "open" or not close or current < close:
            continue
        ticker = row["ticker"]
        if ticker not in outcomes:
            try:
                market = fetch_market(ticker)
                market = market.get("market", market) if isinstance(market, dict) else {}
                outcomes[ticker] = market.get("result") if market.get("status") in {"finalized", "settled"} else None
            except Exception:
                outcomes[ticker] = None
        result = outcomes[ticker]
        if result not in {"yes", "no"}:
            continue
        if "contracts" not in row:
            row.update(status="settled", result=result, settled_at=current.isoformat())
            continue
        won = row["side"] == result
        profit = (row["contracts"] if won else 0) - row["cost_dollars"]
        row.update(status="settled", result=result, won=won, settled_at=current.isoformat(),
                   profit_dollars=round(profit, 8), stress_profit_dollars=round(profit - .01 * row["contracts"], 8))


def cluster_bound(rows, key, alpha):
    buckets = defaultdict(list)
    for row in rows:
        buckets[key(row)].append(row)
    totals = [(sum(r["contracts"] for r in group), sum(r["stress_profit_dollars"] for r in group))
              for group in buckets.values()]
    total = sum(n for n, _ in totals)
    if not total or len(totals) < 2:
        return None
    mean = sum(p for _, p in totals) / total
    # Every stressed return/contract is bounded by [-1.10, 1.00].
    radius = 2.10 * math.sqrt(.5 * math.log(1 / alpha) * sum((n / total) ** 2 for n, _ in totals))
    return round(mean - radius, 8)


def metrics(rows, lane, alpha=.005):
    rows = sorted([r for r in rows if r.get("lane") == lane and r.get("status") == "settled"
                   and r.get("primary_sample")], key=lambda r: r["captured_at"])
    day = lambda r: local_day(r["captured_at"])
    days = {day(r) for r in rows}
    dates = defaultdict(float)
    for r in rows:
        dates[day(r)] += r["stress_profit_dollars"]
    half = len(rows) // 2
    calibration = [r for r in rows if r.get("probability_yes") is not None and r.get("market_probability_yes") is not None]
    return {"lane": lane, "markets": len({r["ticker"] for r in rows}),
            "expiries": len({r["expiry_key"] for r in rows}), "active_dates": len(days),
            "profit_dollars": round(sum(r["profit_dollars"] for r in rows), 6),
            "stress_profit_dollars": round(sum(r["stress_profit_dollars"] for r in rows), 6),
            "day_cluster_lower_per_contract": cluster_bound(rows, day, alpha / 2),
            "expiry_cluster_lower_per_contract": cluster_bound(rows, lambda r: r["expiry_key"], alpha / 2),
            "both_halves_positive": bool(half and sum(r["stress_profit_dollars"] for r in rows[:half]) > 0
                                          and sum(r["stress_profit_dollars"] for r in rows[half:]) > 0),
            "drop_best_day_positive": bool(len(days) > 1 and sum(dates.values()) - max(dates.values()) > 0),
            "calibration_markets": len(calibration),
            "brier_improvement": (sum((number(r["market_probability_yes"]) / 100 - (r["result"] == "yes")) ** 2
                                      - (number(r["probability_yes"]) / 100 - (r["result"] == "yes")) ** 2
                                      for r in calibration) / len(calibration)) if calibration else None}


def fixed_reviews(ledger, now=None):
    current = stamp(now)
    registered = parsed(ledger["registered_at"])
    due = max(0, int((current - registered).total_seconds() // (30 * 86400)))
    for index in range(1, due + 1):
        if any(r.get("policy_hash") == POLICY_HASH and r["index"] == index for r in ledger["reviews"]):
            continue
        cutoff = registered + timedelta(days=30 * index)
        rows = [r for r in ledger["records"] if r.get("policy_hash") == POLICY_HASH
                and (parsed(r.get("settled_at")) or current + timedelta(days=1)) <= cutoff]
        alpha = .05 / (index * (index + 1) * 5)
        results = []
        for lane in PRIMARY:
            result = metrics(rows, lane, alpha)
            cfg = PROTOCOL["review"]
            gates = {"markets": result["markets"] >= cfg["minimum_markets"][lane],
                     "expiries": result["expiries"] >= cfg["minimum_expiries"][lane],
                     "active_dates": result["active_dates"] >= cfg["minimum_active_dates"][lane],
                     "day_lower_positive": number(result["day_cluster_lower_per_contract"], -1) > 0,
                     "expiry_lower_positive": number(result["expiry_cluster_lower_per_contract"], -1) > 0,
                     "both_halves_positive": result["both_halves_positive"],
                     "drop_best_day_positive": result["drop_best_day_positive"],
                     "stress_positive": result["stress_profit_dollars"] > 0}
            result.update(gates=gates, status="ready_for_manual_review" if all(gates.values()) else "continue_shadow")
            results.append(result)
        ledger["reviews"].append({"policy_hash": POLICY_HASH, "index": index,
                                  "cutoff": cutoff.isoformat(), "alpha_per_lane": alpha,
                                  "lanes": results, "automatic_promotion": False})


def summarize(ledger, now=None):
    current = stamp(now)
    rows = [r for r in ledger["records"] if r.get("policy_hash") == POLICY_HASH]
    reviews = [r for r in ledger["reviews"] if r.get("policy_hash") == POLICY_HASH]
    next_index = len(reviews) + 1
    portfolio = [r for r in ledger["records"] if r.get("portfolio_included")]
    return {"version": VERSION, "mode": "shadow_only", "automatic_promotion": False,
            "registered_at": ledger["registered_at"], "policy_hash": POLICY_HASH,
            "generated_at": current.isoformat(), "scan_count": ledger["scan_count"],
            "records": len(rows), "attempts": len(ledger["attempts"]),
            "open": sum(r.get("status") == "open" for r in rows),
            "next_review_at": (parsed(ledger["registered_at"]) + timedelta(days=30 * next_index)).isoformat(),
            "lanes": [metrics(rows, lane) for lane in PRIMARY],
            "last_fixed_review": reviews[-1] if reviews else None,
            "rejections": dict(sorted(ledger["rejection_counts"].items(), key=lambda kv: -kv[1])[:30]),
            "screening_counts": dict(ledger.get("screening_counts") or {}),
            "portfolio": {"entries": len(portfolio), "open": sum(r.get("status") == "open" for r in portfolio),
                "profit_dollars": round(sum(number(r.get("profit_dollars")) for r in portfolio), 6),
                "exposure_dollars": round(sum(r["cost_dollars"] for r in portfolio if r.get("status") == "open"), 6)},
            "diagnostic_controls": dict(Counter(r["lane"] for r in rows if not r.get("primary_sample"))),
            "brti_fixed_observations": len([r for r in ledger["forecasts"] if r.get("policy_hash") == POLICY_HASH]),
            "recent_records": [{k: v for k, v in r.items() if k not in {"fresh_input_snapshot", "confirmation"}} for r in rows[-20:]],
            "review_note": "Only fixed reviews qualify; live enablement requires separate operator approval.",
            "uncertainty_note": PROTOCOL["review"]["assumption"],
            "net_fee_exact": False}


class ResearchStore:
    """One lock and atomic writer shared by scanner and final-minute feed worker."""
    def __init__(self, path, now=None):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.ledger = normalize(json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}, now)
        self.persist()

    def persist(self):
        retain_records(self.ledger, 3000)
        retain_records(self.ledger, 3000, "attempts")
        flush_evidence_archives(self.path, self.ledger)
        temporary = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(self.ledger, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, self.path)

    def scan(self, candidates, refresh, confirm, fetch_market):
        with self.lock:
            working = deepcopy(self.ledger)
            attempt_start = len(working["attempts"])
            rejection_start = dict(working["rejection_counts"])
            screening_start = dict(working["screening_counts"])
        # Network reads must not block the fixed-reading feed worker's writer.
        settle(working, fetch_market)
        capture(working, candidates, refresh, confirm)
        with self.lock:
            for field in ("records", "forecasts"):
                existing = {r["id"]: r for r in self.ledger[field]}
                for row in working[field]:
                    prior = existing.get(row["id"])
                    if prior is None:
                        if field == "records" and row.get("primary_sample"):
                            row["portfolio_reasons"] = portfolio_reasons(self.ledger, row, stamp())
                            row["portfolio_included"] = not row["portfolio_reasons"]
                        self.ledger[field].append(row)
                    elif prior.get("status") == "open" and row.get("status") == "settled":
                        prior.update(row)
            self.ledger["attempts"].extend(working["attempts"][attempt_start:])
            for key, count in working["rejection_counts"].items():
                self.ledger["rejection_counts"][key] = self.ledger["rejection_counts"].get(key, 0) + count - rejection_start.get(key, 0)
            for key, count in working["screening_counts"].items():
                self.ledger["screening_counts"][key] = self.ledger["screening_counts"].get(key, 0) + count - screening_start.get(key, 0)
            self.ledger["scan_count"] += 1
            fixed_reviews(self.ledger)
            self.persist()
            return summarize(self.ledger)

    def brti_tick(self, market, review, cf, book, history, confirm):
        with self.lock:
            before = (len(self.ledger["records"]), len(self.ledger["attempts"]), len(self.ledger["forecasts"]))
            capture_brti(self.ledger, market, review, cf, book, history, confirm)
            after = (len(self.ledger["records"]), len(self.ledger["attempts"]), len(self.ledger["forecasts"]))
            if after != before:
                self.persist()
