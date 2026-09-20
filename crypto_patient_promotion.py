"""ETH patient-entry promotion rules. Pure decisions; no network or order APIs.

The entry definition is the September 14 sizing study. Bankroll-scaled stakes
are a new policy, not a replay of that study's fixed 1--5 contract results.
"""
from copy import deepcopy
from datetime import timedelta
import math
import uuid

from crypto_execution_safety import confirmation_reasons, shard_cash
from crypto_pricing import kalshi_order_fee
from crypto_prospective_shadow import hypothesis, local_day, parsed, quality_reasons


OWNER = "crypto_eth_patient_recovery"
FIELD = "eth_patient_promotion"
VERSION = "eth-patient-bankroll-v1"
ENABLED = "CRYPTO_ETH_PATIENT_ENABLED"


def number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def enabled(settings):
    return str(settings.get(ENABLED, "false")).lower() == "true"


def is_candidate(row):
    return (row.get(FIELD) or {}).get("version") == VERSION


def signal_tier(candidate, side):
    direction = (-1 if candidate.get("market_kind") == "below" else 1) * (1 if side == "yes" else -1)
    venues = [(candidate.get("microstructure") or {}).get(v) or {} for v in ("coinbase", "kraken")]
    flow = min(number(v.get("trade_flow_60s")) * direction for v in venues)
    persistence = min(number(v.get("trade_flow_300s")) * direction for v in venues)
    trades = min(number(v.get("trade_count_60s")) for v in venues)
    quality = number((candidate.get("data_quality") or {}).get("score"))
    spread = number((candidate.get("kalshi_microstructure") or {}).get("spread_yes_cents"), 999)
    if quality < .95 or spread > 1.5:
        return 1
    if flow >= .35 and persistence >= .2 and trades >= 40 and quality >= .98 and spread <= 1:
        return 5 if flow >= .5 and persistence >= .3 and trades >= 60 else 4
    return 3 if flow >= .25 and persistence >= .15 else 2


def signal_review(candidate, now):
    reasons = quality_reasons(candidate, now)
    if candidate.get("market_lane") != "crypto_15m":
        reasons.append("wrong_market_lane")
    rule = hypothesis(candidate, "eth_persistence", now)
    if not rule:
        reasons.append("eth_persistence_absent")
    # NaN must never pass a comparison in the inherited research helpers.
    required = [((candidate.get("data_quality") or {}).get("score"))]
    for venue in ("coinbase", "kraken"):
        row = (candidate.get("microstructure") or {}).get(venue) or {}
        required.extend(row.get(k) for k in ("trade_flow_60s", "trade_flow_300s", "trade_count_60s"))
    if any(number(value, None) is None for value in required):
        reasons.append("nonfinite_or_missing_signal")
    side = (rule or {}).get("side")
    if side and not 35 <= number(candidate.get(side + "_ask"), -1) <= 65:
        reasons.append("price_outside_study_band")
    return side, list(dict.fromkeys(reasons))


def live_rows(portfolio):
    rows = {}
    for row in [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]:
        if row.get("mode") != "live":
            continue
        key = row.get("kalshi_order_id") or row.get("id") or (row.get("ticker"), row.get("placed_at"))
        rows[str(key)] = row
    return list(rows.values())


def risk_context(portfolio, reconciliation, candidate, settings, now):
    """Use verified equity for sizing and shard cash for affordability."""
    reasons = []
    account = reconciliation.get("account") or {}
    age = (now - parsed(reconciliation.get("generated_at"))).total_seconds() if parsed(reconciliation.get("generated_at")) else -1
    if not 0 <= age <= 90:
        reasons.append("reconciliation_stale")
    if any(account.get(key) is not True for key in ("ok", "positions_complete", "orders_complete")):
        reasons.append("account_incomplete")
    if reconciliation.get("unmatched_local_tickers") or reconciliation.get("unmatched_remote_tickers"):
        reasons.append("reconciliation_mismatch")
    if reconciliation.get("delayed_settlement_alert_count"):
        reasons.append("settlement_audit_warning")
    cash = number(account.get("cash_balance"), -1)
    raw = account.get("balance_raw") or {}
    value = account.get("portfolio_value_dollars", raw.get("portfolio_value_dollars"))
    if value is None and raw.get("portfolio_value") is not None:
        value = number(raw["portfolio_value"], -100) / 100
    value = number(value, -1)
    if cash < 0 or value < 0:
        reasons.append("bankroll_unverified")
    bankroll = max(0, cash) + max(0, value)
    available = shard_cash(account, candidate.get("exchange_index"))
    if available is None:
        reasons.append("exchange_cash_unverified")
    available = min(max(0, cash), available or 0)
    rows = live_rows(portfolio)
    opened = [r for r in rows if r.get("status") in {"open", "pending", "resting"}]
    ticker = candidate.get("ticker")
    if any((r.get("ticker") or r.get("kalshi_ticker")) == ticker for r in rows):
        reasons.append("ticker_already_traded")
    if any(str(r.get("ticker") or "").startswith(("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP")) for r in account.get("resting_orders") or []):
        reasons.append("crypto_resting_orders_present")
    for key, count in (
        ("CRYPTO_MAX_OPEN_BETS", len(opened)),
        ("CRYPTO_MAX_OPEN_PER_ASSET", sum(r.get("asset") == "ETH" for r in opened)),
        ("CRYPTO_MAX_OPEN_PER_EVENT", sum(bool(candidate.get("event_ticker")) and r.get("event_ticker") == candidate.get("event_ticker") for r in opened)),
    ):
        cap = number(settings.get(key))
        if cap > 0 and count >= cap:
            reasons.append(key.lower())
    own = [r for r in rows if r.get("strategy_owner") == OWNER]
    settled = []
    for row in own:
        if row.get("status") != "settled":
            continue
        at = parsed(row.get("settled_at"))
        if not at or number(row.get("profit"), None) is None:
            reasons.append("settlement_accounting_incomplete")
        elif at <= now:
            settled.append(row)
    settled.sort(key=lambda r: (parsed(r["settled_at"]), str(r.get("id") or "")))
    pnl = peak = 0.0
    for row in settled:
        pnl += float(row["profit"])
        peak = max(peak, pnl)
    drawdown = max(0, peak - pnl)
    day = local_day(now.isoformat())
    recovery_spent = sum(number((r.get(FIELD) or {}).get("recovery_extra_cost")) for r in own if local_day(r.get("placed_at")) == day)
    exposure = sum(number(r.get("stake")) + number(r.get("fee")) for r in opened)
    daily_loss = sum(max(0, -number(r.get("profit"))) for r in rows if r.get("status") == "settled" and local_day(r.get("settled_at")) == day)
    caps = [number(settings.get("CRYPTO_15M_DAILY_LOSS_CAP")), bankroll * number(settings.get("CRYPTO_15M_DAILY_LOSS_PCT"))]
    caps = [cap for cap in caps if cap > 0]
    loss_room = min(caps) - daily_loss - exposure if caps else available
    if bankroll <= 0 or drawdown >= bankroll * .06:
        reasons.append("drawdown_entry_stop")
    return {"ok": not reasons, "reasons": reasons, "bankroll": bankroll,
            "available_cash": max(0, min(available, loss_room)), "drawdown": drawdown,
            "last_loss": bool(settled and number(settled[-1]["profit"]) < 0),
            "recovery_spent": recovery_spent, "daily_loss": daily_loss, "open_exposure": exposure}


def order_cost(candidate, price, contracts):
    fee = kalshi_order_fee(price, contracts, schedule=candidate.get("fee_schedule") or {}, liquidity_role="taker")
    if not fee.get("authoritative") or not fee.get("exact"):
        return None
    return round(price * contracts / 100 + number(fee.get("conservative_fee_dollars")), 8)


def size_order(candidate, side, risk, quote_for, ceiling, settings, now):
    tier = signal_tier(candidate, side)
    fraction = number(settings.get("CRYPTO_ETH_PATIENT_BASE_STAKE_PCT", 1), -1) / 100
    if not 0 < fraction <= 1:
        return None, "invalid_stake_fraction"
    base = risk["bankroll"] * fraction
    multiplier = tier / 3
    dd = risk["drawdown"] / max(risk["bankroll"], 1e-9)
    if dd >= .06:
        return None, "drawdown_entry_stop"
    if dd >= .04:
        multiplier = 1 / 3
    elif dd >= .02:
        multiplier = max(1, tier // 2) / 3
    target = base * multiplier
    available = risk["available_cash"]
    upper = int(max(0, min(target, available)) / .35)

    def priced(q):
        quote = quote_for(q)
        price = math.ceil(number(quote.get("full_size_entry_price_cents"), 100))
        if confirmation_reasons(quote, 1, now) or quote.get("source") != "kalshi_rest_orderbook":
            return None
        if not 35 <= price <= min(65, ceiling) or number(quote.get("available_contracts")) < q:
            return None
        cost = order_cost(candidate, price, q)
        if cost is None:
            return None
        return {"contracts": q, "price_cents": price, "cost": cost, "confirmation": deepcopy(quote)}

    low, high, best = 1, upper, None
    while low <= high:
        mid = (low + high) // 2
        entry = priced(mid)
        if entry and entry["cost"] <= min(target, available) + 1e-9:
            best, low = entry, mid + 1
        else:
            high = mid - 1
    if best is None:
        return None, "no_affordable_confirmed_contract"
    extra = 0.0
    if tier == 4 and risk["last_loss"] and dd < .02:
        recovery = priced(best["contracts"] + 1)
        # Keep recovery to one contract, with at most 1% equity added per day.
        if recovery:
            extra = recovery["cost"] - best["cost"]
            if recovery["cost"] <= available + 1e-9 and risk["recovery_spent"] + extra <= risk["bankroll"] * .01 + 1e-9:
                best = recovery
            else:
                extra = 0.0
    best.update(base_budget=round(base, 8), target_budget=round(target, 8), signal_tier=tier,
                multiplier=multiplier, recovery_extra_cost=round(extra, 8), recovery_extra_contracts=int(extra > 0),
                bankroll=risk["bankroll"], principal_stake=round(best["contracts"] * best["price_cents"] / 100, 2))
    return best, "eligible"


class PatientPromotion:
    def __init__(self, portfolio):
        state = portfolio.setdefault(FIELD, {"version": VERSION, "records": {}})
        if not isinstance(state, dict) or state.get("version") != VERSION or not isinstance(state.get("records"), dict):
            raise ValueError("patient_promotion_state_invalid")
        self.state = state

    def observe(self, candidate, quote_for, now):
        ticker = candidate.get("ticker")
        if ticker in self.state["records"]:
            return self.state["records"][ticker]
        side, reasons = signal_review(candidate, now)
        if reasons or not ticker:
            return None
        signal_price = number(candidate.get(side + "_ask"))
        quote = quote_for(3)
        anchor = number(quote.get("full_size_entry_price_cents"), 100)
        confirmed = (not confirmation_reasons(quote, 1, now) and quote.get("source") == "kalshi_rest_orderbook"
                     and number(quote.get("available_contracts")) >= 3 and 35 <= anchor <= min(65, signal_price)
                     and order_cost(candidate, anchor, 3) is not None)
        row = {"ticker": ticker, "side": side, "observed_at": now.isoformat(),
               "deadline": (now + timedelta(seconds=60)).isoformat(), "status": "waiting",
               "anchor_cents": anchor if confirmed else signal_price,
               "anchor_source": "confirmed_three_contract_quote" if confirmed else "signal_no_baseline_fill"}
        self.state["records"][ticker] = row
        return row

    def prepare(self, candidate, quote_for, risk, settings, now):
        row = self.state["records"].get(candidate.get("ticker"))
        if not row or row["status"] != "waiting":
            return None
        if now > parsed(row["deadline"]):
            row.update(status="expired", reason="patient_deadline_elapsed")
            return None
        side, reasons = signal_review(candidate, now)
        if reasons or side != row["side"] or not risk["ok"]:
            row["reason"] = ",".join(reasons + risk["reasons"]) or "signal_changed"
            return None
        size, reason = size_order(candidate, side, risk, quote_for, row["anchor_cents"] - 1, settings, now)
        row["reason"] = reason
        if not size:
            return None
        result = deepcopy(candidate)
        for key in ("crypto_live_campaign", "crypto_units", "spot_flow_live_pilot", "shared_recovery", "phase_two",
                    "small_edge", "selective_edge", "live_core", "live_quote_refresh", "bot_number"):
            result.pop(key, None)
        result.update(side=side, entry_price=size["price_cents"], strategy_owner=OWNER,
                      recovery_context={"active": False}, skip_reasons=[], decision="eligible")
        result[FIELD] = {**deepcopy(row), **size, "version": VERSION, "qualified_at": now.isoformat(),
                         "client_order_id": "crypto-patient-" + uuid.uuid4().hex}
        return result


def order_reasons(settings, portfolio, candidate, now):
    """Last gate, including durable intent and freshness, before reservation."""
    review = candidate.get(FIELD) or {}
    state = portfolio.get(FIELD) or {}
    intent = (state.get("records") or {}).get(candidate.get("ticker")) or {}
    reasons = []
    if not enabled(settings):
        reasons.append("patient_promotion_disabled")
    if state.get("version") != VERSION or intent.get("status") != "submitting" or intent.get("client_order_id") != review.get("client_order_id"):
        reasons.append("patient_order_intent_missing")
    if intent.get("side") != candidate.get("side") or review.get("side") != candidate.get("side"):
        reasons.append("patient_side_changed")
    at, deadline = parsed(review.get("qualified_at")), parsed(review.get("deadline"))
    if not at or not 0 <= (now - at).total_seconds() <= 1 or not deadline or now > deadline:
        reasons.append("patient_confirmation_expired")
    side, signal_errors = signal_review(candidate, now)
    reasons.extend(signal_errors)
    if side != candidate.get("side"):
        reasons.append("patient_signal_changed")
    if not 35 <= number(candidate.get("entry_price")) <= min(65, number(intent.get("anchor_cents")) - 1):
        reasons.append("patient_price_chased")
    if number(review.get("contracts")) < 1:
        reasons.append("patient_contracts_missing")
    reasons.extend(confirmation_reasons(review.get("confirmation") or {}, 1, now))
    cost = order_cost(candidate, number(candidate.get("entry_price")), number(review.get("contracts")))
    if cost is None or abs(cost - number(review.get("cost"), -1)) > 1e-9:
        reasons.append("patient_cost_changed")
    if number(candidate.get("entry_price")) != number(review.get("price_cents")):
        reasons.append("patient_price_changed")
    return list(dict.fromkeys(reasons))
