"""Bounded unit additions, using settled bot results and a durable small ledger.

This module never submits orders. Losses do not change the dollar value of 1U.
All amounts here are units of stake; the executor still reserves trading fees.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CHICAGO = ZoneInfo("America/Chicago")
VERSION = "sports-modest-recovery-v1"
LEDGER_KEY = "sports_modest_recovery_ledger"
LANES = ("aibetpicks", "scanner")
POLICY = {
    "step_units": 0.5, "max_add_units": 2.0,
    "lane_daily_extra_cap": 3.0, "combined_daily_extra_cap": 4.0,
    "combined_open_extra_cap": 2.0, "carryover_cap_units": 3.0,
    "pause_drawdown_units": 6.0,
}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def stamp(value):
    try:
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (value if value.tzinfo else value.replace(tzinfo=CHICAGO)).astimezone(CHICAGO)
    except (ValueError, TypeError):
        return None


def lane_for(row):
    if row.get("mode") != "live":
        return None
    # Manual positions never fund or trigger either bot's recovery.
    if row.get("source") in {"user_manual", "user_bet"} or row.get("strategy_owner") in {"user_bet", "manual_live_import"}:
        return None
    if row.get("source") == "aibetpicks" or row.get("strategy_owner") == "aibetpicks":
        return "aibetpicks"
    if row.get("source") == "edge_scanner" and row.get("strategy_owner") == "live_campaign":
        return "scanner"
    return None


def enabled(engine):
    return str(engine.settings_file_value("SPORTS_MODEST_RECOVERY_ENABLED", "false")).lower() == "true"


def row_key(row):
    order = row.get("live_order") or {}
    identity = ((order.get("request") or {}).get("client_order_id")
                or (order.get("response") or {}).get("order_id") or row.get("id"))
    if identity:
        return str(identity)
    # Older scanner positions have no row ID. Side/time distinguish real fills.
    identity = [row.get(k) for k in ("kalshi_ticker", "placed_at", "order_side", "stake", "contracts")]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def entries(portfolio, now=None):
    """Merge live rows into retained entries; no file writes or archive reads.

    save_portfolio retains this ledger across history trimming/log compression.
    A late settlement keeps its original entry unit value.
    """
    now = (now or datetime.now(CHICAGO)).astimezone(CHICAGO)
    cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    rows = {str(k): dict(v) for k, v in ((portfolio.get(LEDGER_KEY) or {}).get("entries") or {}).items()
            if isinstance(v, dict) and v.get("lane") in LANES}
    for row in [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]:
        lane = lane_for(row)
        if lane is None or row.get("status") not in {"open", "pending", "resting", "settled"}:
            continue
        placed = stamp(row.get("placed_at"))
        settled = stamp(row.get("settled_at") or row.get("settlement_ts"))
        is_open = row.get("status") != "settled"
        if not placed or placed > now or (not is_open and (not settled or settled > now)):
            continue
        if not is_open and max(placed, settled) < cutoff:
            continue
        units = row.get("sports_units") or {}
        recovery = units.get("recovery") or {}
        entry_unit = number(row.get("unit_size") or units.get("unit_size"))
        profit = number(row.get("profit"))
        pnl = profit / entry_unit if not is_open and profit is not None and entry_unit and entry_unit > 0 else None
        key = row_key(row)
        if is_open and rows.get(key, {}).get("settled_at"):
            continue  # Never let a stale duplicate open row undo a settlement.
        rows[key] = {"lane": lane, "placed_at": placed.isoformat(),
                     "settled_at": settled.isoformat() if not is_open else None,
                     "profit_units": pnl, "open": is_open,
                     "extra_units": max(0, number(recovery.get("added_units")) or 0),
                     "game_key": row.get("game_key") or row.get("event_id") or row.get("event_ticker")}
    return {k: v for k, v in rows.items() if v.get("open") or any(
        parsed and cutoff <= parsed <= now for parsed in (stamp(v.get("placed_at")), stamp(v.get("settled_at"))))}


def retain_ledger(portfolio, now=None):
    portfolio[LEDGER_KEY] = {"version": VERSION, "entries": entries(portfolio, now)}


def summary(portfolio, now=None):
    now = (now or datetime.now(CHICAGO)).astimezone(CHICAGO)
    today, yesterday = now.date(), (now - timedelta(days=1)).date()
    records = list(entries(portfolio, now).values())
    daily_extra = sum(number(r.get("extra_units")) or 0 for r in records if (stamp(r.get("placed_at")) or now).date() == today)
    open_extra = sum(number(r.get("extra_units")) or 0 for r in records if r.get("open"))
    lanes = {}
    for lane in LANES:
        own = [r for r in records if r["lane"] == lane]
        settled = [r for r in own if stamp(r.get("settled_at"))]
        today_pnl = sum(number(r.get("profit_units")) or 0 for r in settled if stamp(r["settled_at"]).date() == today)
        previous_pnl = sum(number(r.get("profit_units")) or 0 for r in settled if stamp(r["settled_at"]).date() == yesterday)
        carry = min(POLICY["carryover_cap_units"], max(0, -previous_pnl))
        # Profits earlier today offset later losses; previous profits do not
        # create a license to add risk today. A previous severe loss day pauses
        # next-day additions as well, rather than hiding it behind the carry cap.
        drawdown = max(0, carry - today_pnl)
        incomplete = any(number(r.get("profit_units")) is None for r in settled)
        paused = incomplete or drawdown >= POLICY["pause_drawdown_units"] or previous_pnl <= -POLICY["pause_drawdown_units"]
        lanes[lane] = {"today_profit_units": round(today_pnl, 6), "previous_day_profit_units": round(previous_pnl, 6),
                       "carryover_units": round(carry, 6), "drawdown_units": round(drawdown, 6),
                       "settled_losses": sum(1 for r in settled if (number(r.get("profit_units")) or 0) < -0.01),
                       "daily_extra_units": round(sum(number(r.get("extra_units")) or 0 for r in own
                           if (stamp(r.get("placed_at")) or now).date() == today), 6),
                       "paused": paused, "incomplete_accounting": incomplete}
    return {"version": VERSION, "date": today.isoformat(), "timezone": "America/Chicago",
            "policy": dict(POLICY), "combined_daily_extra_units": round(daily_extra, 6),
            "combined_open_extra_units": round(open_extra, 6), "lanes": lanes}


def review(portfolio, candidate, *, lane, base_units, maximum_units, now=None, is_enabled=False):
    """Approve a small addition only; never make an ineligible base bet valid."""
    now = (now or datetime.now(CHICAGO)).astimezone(CHICAGO)
    base, maximum = number(base_units), number(maximum_units)
    result = {"version": VERSION, "enabled": bool(is_enabled), "lane": lane,
              "base_units": base, "added_units": 0.0, "target_units": base,
              "reason": "disabled", "reviewed_at": now.isoformat(timespec="seconds")}
    if not is_enabled:
        return result
    result["reason"] = "invalid_base_size"
    if lane not in LANES or base is None or base <= 0 or maximum is None or maximum < base:
        return result
    state = summary(portfolio, now)
    own = state["lanes"][lane]
    result.update(drawdown_units=own["drawdown_units"], carryover_units=own["carryover_units"],
                  daily_extra_units=own["daily_extra_units"], combined_daily_extra_units=state["combined_daily_extra_units"],
                  combined_open_extra_units=state["combined_open_extra_units"])
    result["reason"] = "loss_additions_paused" if own["paused"] else "no_settled_deficit"
    if own["paused"] or own["drawdown_units"] < 0.5:
        return result
    result["reason"] = "existing_position_no_recovery_topup"
    if (number((candidate.get("sports_units") or {}).get("existing_units")) or 0) > 0:
        return result
    game = candidate.get("game_key") or candidate.get("event_id") or candidate.get("event_ticker")
    if game and any(r.get("open") and r.get("game_key") == game and (number(r.get("extra_units")) or 0) > 0
                    for r in entries(portfolio, now).values()):
        result["reason"] = "event_recovery_already_open"
        return result
    price = number(candidate.get("entry_price"))
    result["reason"] = "price_outside_recovery_range"
    if price is None or not 25 <= price <= 70:
        return result
    proposed = 1.0 if own["drawdown_units"] >= 2 else 0.5
    if own["drawdown_units"] >= 4 and own["settled_losses"] >= 3 and base >= 1 and 35 <= price <= 65:
        proposed = 2.0
    if lane == "scanner":
        edge = number(candidate.get("edge"))
        result["reason"] = "scanner_edge_not_positive"
        if edge is None or edge <= 0 or candidate.get("skip_reasons"):
            return result
        proposed = min(proposed, 2.0 if edge >= 2 else 1.0 if edge >= 1 else 0.5)
        probability = candidate.get("probability_sizing") or {}
        # Keep explicit probability risk reductions intact.
        if probability.get("active") and float(probability.get("target_units") or 0) < base:
            result["reason"] = "probability_risk_reduction"
            return result
    room = min(proposed, 2 * base, maximum - base,
               POLICY["lane_daily_extra_cap"] - own["daily_extra_units"],
               POLICY["combined_daily_extra_cap"] - state["combined_daily_extra_units"],
               POLICY["combined_open_extra_cap"] - state["combined_open_extra_units"])
    added = max(0, math.floor((room + 1e-9) / POLICY["step_units"]) * POLICY["step_units"])
    result.update(added_units=added, target_units=round(base + added, 6),
                  reason="modest_recovery_addition" if added else "recovery_budget_or_market_cap")
    return result


def resize(review, target_units, reason):
    """Execution/exposure may remove the addition, never inflate its approval."""
    base = number(review.get("base_units")) or 0
    return {**review, "target_units": target_units,
            "added_units": round(max(0, min(number(review.get("added_units")) or 0, target_units - base)), 6),
            "reason": reason}
