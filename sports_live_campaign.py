from __future__ import annotations

import math
from datetime import datetime


OWNER = "live_campaign"
ROLES = {"primary", "support"}


def campaign_bot_number(row, default=1):
    """Return the persistent campaign lane number; legacy rows belong to Bot 1."""
    campaign = (row or {}).get("live_campaign") or {}
    value = campaign.get("bot_number", (row or {}).get("bot_number", default))
    try:
        return max(1, int(float(value or default)))
    except (TypeError, ValueError):
        return max(1, int(default or 1))


def money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _row_date(row, date_from_iso):
    campaign = row.get("live_campaign") or {}
    explicit = str(campaign.get("campaign_date") or "")
    if explicit:
        return explicit
    return date_from_iso(row.get("placed_at") or row.get("created_at"))


def _campaign_rows(portfolio, campaign_date, date_from_iso, campaign_id="", bot_number=1):
    campaign_id = str(campaign_id or "").strip()
    rows = []
    for row in [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]:
        if row.get("strategy_owner") != OWNER:
            continue
        campaign = row.get("live_campaign") or {}
        if campaign.get("role") not in ROLES:
            continue
        if campaign_bot_number(row) != campaign_bot_number({"bot_number": bot_number}):
            continue
        if campaign_id and str(campaign.get("campaign_id") or "") != campaign_id:
            continue
        if _row_date(row, date_from_iso) == campaign_date:
            rows.append(row)
    return rows


def _is_open(row):
    return str(row.get("status") or "").lower() in {"open", "pending", "resting"}


def _is_settled(row):
    return str(row.get("status") or "").lower() == "settled"


def _is_live_bot_position(row):
    if not _is_open(row) or str(row.get("mode") or "").lower() != "live":
        return False
    owner = str(row.get("strategy_owner") or "")
    return owner not in {"", "user_bet", "manual_live_import"}


def _is_assumed_loss_open(row):
    return bool(
        _is_open(row)
        and row.get("strategy_owner") == OWNER
        and (row.get("live_campaign") or {}).get("assumed_loss")
    )


def _assumed_loss_ledger_row(row, fee_rate):
    campaign = row.get("live_campaign") or {}
    stored_amount = campaign.get("assumed_loss_amount")
    amount = money(stored_amount)
    if amount <= 0:
        fee = (
            money(row.get("fee"))
            if row.get("fee") is not None
            else _estimated_fee(row.get("stake"), row.get("entry_price"), fee_rate)
        )
        amount = money(money(row.get("stake")) + fee)
    assumed_at = str(campaign.get("assumed_loss_at") or row.get("placed_at") or "")
    return {
        **row,
        "status": "settled",
        "result": "LOSS",
        "profit": money(-amount),
        "settled_at": assumed_at,
        "live_campaign": {
            **campaign,
            "assumed_loss_ledger": True,
        },
    }


def _cumulative_targets(goals):
    total = 0.0
    targets = []
    for goal in goals:
        total = money(total + goal)
        targets.append(total)
    return targets


def _estimated_fee(stake, price, fee_rate):
    price_fraction = max(0.0, min(1.0, float(price or 0) / 100.0))
    return money(float(stake or 0) * float(fee_rate or 0) * (1.0 - price_fraction))


def contract_fee(contracts, price, fee_rate):
    """Return the exchange-style aggregate taker fee rounded up to one cent."""
    contracts = max(0, int(contracts or 0))
    price_fraction = max(0.0, min(1.0, float(price or 0) / 100.0))
    raw_fee = float(fee_rate or 0) * contracts * price_fraction * (1.0 - price_fraction)
    return math.ceil(max(0.0, raw_fee) * 100.0 - 1e-12) / 100.0


def contract_win_profit(contracts, price, fee_rate):
    contracts = max(0, int(contracts or 0))
    price_fraction = max(0.0, min(1.0, float(price or 0) / 100.0))
    return money(contracts * (1.0 - price_fraction) - contract_fee(contracts, price, fee_rate))


def contracts_for_target_profit(target_profit, price, fee_rate):
    """Find the minimum whole contracts whose win profit reaches the target."""
    target_profit = money(target_profit)
    price_fraction = max(0.0, min(1.0, float(price or 0) / 100.0))
    if target_profit <= 0 or price_fraction <= 0 or price_fraction >= 1:
        return 0
    approximate_profit = max(
        0.0001,
        (1.0 - price_fraction)
        - float(fee_rate or 0) * price_fraction * (1.0 - price_fraction),
    )
    contracts = max(1, int(math.ceil(target_profit / approximate_profit - 1e-12)))
    while contract_win_profit(contracts, price, fee_rate) + 1e-9 < target_profit:
        contracts += 1
    while contracts > 1 and contract_win_profit(contracts - 1, price, fee_rate) + 1e-9 >= target_profit:
        contracts -= 1
    return contracts


def maximum_contracts_for_caps(price, fee_rate, stake_cap, loss_remaining):
    price_fraction = max(0.0, min(1.0, float(price or 0) / 100.0))
    if price_fraction <= 0 or price_fraction >= 1:
        return 0
    stake_cap = max(0.0, float(stake_cap or 0))
    loss_remaining = max(0.0, float(loss_remaining or 0))
    contracts = max(0, int(math.floor(stake_cap / price_fraction + 1e-9)))
    while contracts > 0:
        cost = contracts * price_fraction
        if cost + contract_fee(contracts, price, fee_rate) <= loss_remaining + 1e-9:
            break
        contracts -= 1
    return contracts


def _daily_bot_rows(portfolio, campaign_date, date_from_iso, bot_number=1):
    rows = []
    for row in [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]:
        if str(row.get("strategy_owner") or "") != OWNER:
            continue
        if campaign_bot_number(row) != campaign_bot_number({"bot_number": bot_number}):
            continue
        if _is_open(row):
            rows.append(row)
            continue
        settled_date = date_from_iso(row.get("settled_at") or row.get("placed_at") or row.get("created_at"))
        if _is_settled(row) and settled_date == campaign_date:
            rows.append(row)
    return rows


def build_campaign_context(
    portfolio,
    campaign_date,
    date_from_iso,
    *,
    cycle_goals=(20.0, 10.0, 5.0),
    max_open=3,
    daily_loss_cap=300.0,
    daily_loss_cap_pct=0.0,
    fee_rate=0.07,
    goal_tolerance=0.05,
    partial_recovery_enabled=False,
    full_recovery_losses=2,
    partial_recovery_fraction=0.50,
    campaign_id="",
    bot_number=1,
):
    bot_number = campaign_bot_number({"bot_number": bot_number})
    goals = tuple(money(goal) for goal in cycle_goals if money(goal) > 0)
    targets = _cumulative_targets(goals)
    campaign_id = str(campaign_id or "").strip()
    rows = _campaign_rows(
        portfolio,
        campaign_date,
        date_from_iso,
        campaign_id=campaign_id,
        bot_number=bot_number,
    )
    settled = [row for row in rows if _is_settled(row)]
    open_rows = [row for row in rows if _is_open(row)]
    assumed_loss_open_rows = [row for row in open_rows if _is_assumed_loss_open(row)]
    assumed_loss_ledger_rows = [
        _assumed_loss_ledger_row(row, fee_rate)
        for row in assumed_loss_open_rows
    ]
    ledger_rows = [*settled, *assumed_loss_ledger_rows]
    primary_settled = [
        row for row in ledger_rows
        if (row.get("live_campaign") or {}).get("role") == "primary"
        and str(row.get("result") or "").upper() in {"WIN", "LOSS"}
    ]
    primary_settled.sort(key=lambda row: str(row.get("settled_at") or row.get("placed_at") or ""))
    primary_wins = sum(1 for row in primary_settled if str(row.get("result") or "").upper() == "WIN")
    consecutive_primary_losses = 0
    for row in reversed(primary_settled):
        if str(row.get("result") or "").upper() != "LOSS":
            break
        consecutive_primary_losses += 1

    realized_profit = money(sum(money(row.get("profit")) for row in settled))
    assumed_loss_profit = money(sum(money(row.get("profit")) for row in assumed_loss_ledger_rows))
    ledger_profit = money(realized_profit + assumed_loss_profit)
    goal_tolerance = max(0.0, money(goal_tolerance))
    # Replay settlements in order. Each cycle owns a fresh profit ledger, and
    # one settlement can complete at most one cycle. Any oversized win becomes
    # the next cycle's baseline instead of silently completing extra cycles.
    ordered_settled = sorted(
        ledger_rows,
        key=lambda row: str(row.get("settled_at") or row.get("placed_at") or ""),
    )
    cycle_index = 0
    cycle_start_profit = 0.0
    running_profit = 0.0
    current_cycle_rows = []
    for row in ordered_settled:
        running_profit = money(running_profit + money(row.get("profit")))
        current_cycle_rows.append(row)
        if (
            cycle_index < len(goals)
            and money(running_profit - cycle_start_profit) + goal_tolerance
            >= goals[cycle_index]
        ):
            cycle_index += 1
            cycle_start_profit = running_profit
            current_cycle_rows = []
    complete = bool(goals) and cycle_index >= len(goals)
    cycle_goal = 0.0 if complete or not goals else goals[cycle_index]
    cycle_realized_profit = money(ledger_profit - cycle_start_profit)
    cycle_target = 0.0 if complete else money(cycle_start_profit + cycle_goal)
    target_remaining = (
        0.0
        if complete
        else money(max(0.0, cycle_goal - cycle_realized_profit))
    )
    cycle_primary_rows = [
        row
        for row in current_cycle_rows
        if (row.get("live_campaign") or {}).get("role") == "primary"
        and str(row.get("result") or "").upper() in {"WIN", "LOSS"}
    ]
    cycle_loss_count = sum(
        1
        for row in cycle_primary_rows
        if str(row.get("result") or "").upper() == "LOSS"
    )
    cycle_win_count = sum(
        1
        for row in cycle_primary_rows
        if str(row.get("result") or "").upper() == "WIN"
    )
    full_recovery_losses = max(0, int(full_recovery_losses or 0))
    partial_recovery_fraction = max(
        0.0,
        min(1.0, float(partial_recovery_fraction or 0)),
    )
    partial_recovery_active = bool(
        partial_recovery_enabled
        and not complete
        and cycle_loss_count >= full_recovery_losses
        and cycle_realized_profit < cycle_goal
    )
    all_open_bets = portfolio.get("bets") or []
    bot_live_open = [
        row
        for row in all_open_bets
        if _is_live_bot_position(row)
        and row.get("strategy_owner") == OWNER
        and campaign_bot_number(row) == bot_number
    ]
    bot_live_assumed_loss_open = [row for row in bot_live_open if _is_assumed_loss_open(row)]
    bot_live_active_open = [row for row in bot_live_open if not _is_assumed_loss_open(row)]
    any_open_primary = any(
        _is_open(row)
        and row.get("strategy_owner") == OWNER
        and campaign_bot_number(row) == bot_number
        and (row.get("live_campaign") or {}).get("role") == "primary"
        and not _is_assumed_loss_open(row)
        for row in all_open_bets
    )
    daily_rows = _daily_bot_rows(portfolio, campaign_date, date_from_iso, bot_number)
    daily_settled = [row for row in daily_rows if _is_settled(row)]
    daily_open = [row for row in daily_rows if _is_open(row)]
    bot_daily_realized_profit = money(sum(money(row.get("profit")) for row in daily_settled))
    bot_daily_open_risk = money(sum(
        money(row.get("stake"))
        + (
            money(row.get("fee"))
            if row.get("fee") is not None
            else _estimated_fee(row.get("stake"), row.get("entry_price"), fee_rate)
        )
        for row in daily_open
    ))
    configured_daily_loss_cap = money(daily_loss_cap)
    daily_loss_cap_pct = max(0.0, float(daily_loss_cap_pct or 0))
    # Reconstruct the start-of-day bankroll so the percentage limit is stable:
    # add currently committed stake back and remove today's settled P/L.
    # Otherwise a loss would shrink both bankroll and the loss allowance, while
    # a win would silently increase the amount the bot may lose later that day.
    bankroll_base = money(
        money(portfolio.get("balance"))
        + sum(money(row.get("stake")) for row in daily_open)
        - bot_daily_realized_profit
    )
    percentage_daily_loss_cap = money(bankroll_base * daily_loss_cap_pct)
    available_caps = [
        cap
        for cap in (configured_daily_loss_cap, percentage_daily_loss_cap)
        if cap > 0
    ]
    daily_loss_cap = money(min(available_caps)) if available_caps else 0.0
    daily_loss_cap_enabled = daily_loss_cap > 0
    daily_loss_remaining = (
        money(max(0.0, daily_loss_cap + bot_daily_realized_profit - bot_daily_open_risk))
        if daily_loss_cap_enabled
        else 0.0
    )
    daily_loss_cap_hit = daily_loss_cap_enabled and daily_loss_remaining <= 0
    status = "complete" if complete else "daily_loss_cap" if daily_loss_cap_hit else "active"
    return {
        "enabled": True,
        "owner": OWNER,
        "bot_number": bot_number,
        "campaign_date": campaign_date,
        "campaign_id": campaign_id,
        "status": status,
        "complete": complete,
        "cycle_goals": list(goals),
        "cycle_targets": targets,
        "cycle_index": cycle_index,
        "cycle_number": min(cycle_index + 1, len(goals)) if goals else 0,
        "cycle_goal": money(cycle_goal),
        "cycle_target": money(cycle_target),
        "cycle_start_profit": money(cycle_start_profit),
        "cycle_realized_profit": money(cycle_realized_profit),
        "cycle_loss_count": cycle_loss_count,
        "cycle_win_count": cycle_win_count,
        "progression_mode": (
            "separate_cycle_profit_targets" if goals else "always_on_unit_portfolio"
        ),
        "goals_enabled": bool(goals),
        "partial_recovery_enabled": bool(partial_recovery_enabled),
        "partial_recovery_active": partial_recovery_active,
        "partial_recovery_fraction": round(partial_recovery_fraction, 6),
        "full_recovery_losses": full_recovery_losses,
        "target_remaining": target_remaining,
        "goal_tolerance": goal_tolerance,
        "primary_wins": primary_wins,
        "consecutive_primary_losses": consecutive_primary_losses,
        "realized_profit": realized_profit,
        "realized_drawdown": money(abs(min(0.0, realized_profit))),
        "ledger_profit": ledger_profit,
        "ledger_drawdown": money(abs(min(0.0, ledger_profit))),
        "assumed_loss_profit": assumed_loss_profit,
        "assumed_loss_amount": money(abs(assumed_loss_profit)),
        "assumed_loss_open_count": len(assumed_loss_open_rows),
        "settled_count": len(settled),
        "ledger_count": len(ledger_rows),
        "campaign_open_count": len(open_rows),
        "bot_live_open_count": len(bot_live_open),
        "bot_live_active_open_count": len(bot_live_active_open),
        "bot_live_assumed_loss_open_count": len(bot_live_assumed_loss_open),
        "max_open": int(max_open),
        "open_primary": any_open_primary,
        "daily_loss_cap": daily_loss_cap,
        "daily_loss_cap_enabled": daily_loss_cap_enabled,
        "configured_daily_loss_cap": configured_daily_loss_cap,
        "daily_loss_cap_pct": round(daily_loss_cap_pct, 6),
        "daily_loss_cap_source": (
            "minimum_of_configured_and_bankroll_pct"
            if configured_daily_loss_cap > 0 and percentage_daily_loss_cap > 0
            else "bankroll_pct"
            if percentage_daily_loss_cap > 0
            else "configured_amount"
        ),
        "daily_loss_bankroll_base": bankroll_base,
        "percentage_daily_loss_cap": percentage_daily_loss_cap,
        "daily_loss_cap_hit": daily_loss_cap_hit,
        "bot_daily_realized_profit": bot_daily_realized_profit,
        "bot_daily_open_risk": bot_daily_open_risk,
        "daily_loss_remaining": daily_loss_remaining,
    }


def _metrics(candidate):
    return {
        "edge": float(candidate.get("edge") or 0),
        "confidence": float(candidate.get("confidence_score") or candidate.get("confidence") or 0),
        "pro_score": float((candidate.get("pro_review") or {}).get("score") or candidate.get("pro_score") or 0),
        "final_score": float(candidate.get("final_bet_score") or (candidate.get("final_score_review") or {}).get("score") or 0),
    }


def _conservative_edge(candidate):
    pricing = candidate.get("pricing_v2") or {}
    guarded_values = (pricing.get("guarded_nominal_review") or {}).get("values") or {}
    for value in (
        pricing.get("net_conservative_edge_pp"),
        guarded_values.get("conservative_net_edge_pp"),
        candidate.get("net_conservative_edge_pp"),
    ):
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _timing_bucket(candidate):
    explicit = str(
        candidate.get("bet_timing_bucket")
        or candidate.get("timing_bucket")
        or (candidate.get("pricing_v2") or {}).get("timing_bucket")
        or ""
    ).lower()
    if "early" in explicit:
        return "early_live"
    if "mid" in explicit:
        return "mid_live"
    if "late" in explicit:
        return "late_live"
    try:
        minutes = float(candidate.get("minutes_since_start"))
    except (TypeError, ValueError):
        return "live"
    if minutes <= 60:
        return "early_live"
    if minutes <= 150:
        return "mid_live"
    return "late_live"


def _total_market_review(
    candidate,
    *,
    min_conservative_edge,
    line_tolerance,
    min_book_families,
    min_sharp_books,
):
    pricing = candidate.get("pricing_v2") or {}
    consensus = pricing.get("consensus") or {}
    failures = []
    try:
        market_line = float(candidate.get("market_line"))
        book_line = float(candidate.get("book_line"))
    except (TypeError, ValueError):
        market_line = None
        book_line = None
        failures.append("campaign_total_line_missing")
    if (
        market_line is not None
        and book_line is not None
        and abs(market_line - book_line) > max(0.0, float(line_tolerance or 0))
    ):
        failures.append("campaign_total_line_mismatch")

    direction_text = " ".join(
        str(candidate.get(field) or "")
        for field in ("total_side", "selected_team", "selection", "side")
    ).lower()
    direction = "under" if "under" in direction_text else "over" if "over" in direction_text else ""
    if not direction:
        failures.append("campaign_total_direction_missing")

    conservative_edge = _conservative_edge(candidate)
    if conservative_edge is None or conservative_edge + 1e-9 < float(min_conservative_edge or 0):
        failures.append("campaign_total_conservative_edge")
    try:
        families = int(
            candidate.get("independent_book_family_count")
            if candidate.get("independent_book_family_count") is not None
            else consensus.get("independent_family_count") or 0
        )
    except (TypeError, ValueError):
        families = 0
    try:
        sharp_books = int(
            candidate.get("sharp_book_count")
            if candidate.get("sharp_book_count") is not None
            else consensus.get("sharp_book_count") or 0
        )
    except (TypeError, ValueError):
        sharp_books = 0
    if families < int(min_book_families or 0):
        failures.append("campaign_total_independent_books")
    if sharp_books < int(min_sharp_books or 0):
        failures.append("campaign_total_sharp_books")
    return {
        "ok": not failures,
        "failures": failures,
        "direction": direction,
        "market_line": market_line,
        "book_line": book_line,
        "line_tolerance": float(line_tolerance or 0),
        "conservative_edge": conservative_edge,
        "min_conservative_edge": float(min_conservative_edge or 0),
        "independent_book_families": families,
        "min_independent_book_families": int(min_book_families or 0),
        "sharp_books": sharp_books,
        "min_sharp_books": int(min_sharp_books or 0),
    }


def quality_tier(candidate, thresholds):
    metrics = _metrics(candidate)
    for tier in ("elite", "strong", "qualified"):
        required = thresholds[tier]
        if all(metrics[key] >= float(required[key]) for key in metrics):
            return tier, metrics
    return "", metrics


def _same_open_market(portfolio, candidate, bot_number=1):
    """Match the same contract, not merely another market in the same game."""
    ticker = str(candidate.get("kalshi_ticker") or "").strip()
    event_ticker = str(candidate.get("event_ticker") or "").strip()
    market_type = str(candidate.get("market_type") or "").strip().lower()
    market_line = candidate.get("market_line")
    order_side = str(candidate.get("order_side") or "").strip().lower()
    if not ticker and not (event_ticker and market_type):
        return False
    for row in portfolio.get("bets") or []:
        if not _is_live_bot_position(row):
            continue
        if row.get("strategy_owner") != OWNER or campaign_bot_number(row) != bot_number:
            continue
        if ticker and str(row.get("kalshi_ticker") or "").strip() == ticker:
            return True
        if (
            not ticker
            and event_ticker
            and str(row.get("event_ticker") or "").strip() == event_ticker
            and str(row.get("market_type") or "").strip().lower() == market_type
            and row.get("market_line") == market_line
            and str(row.get("order_side") or "").strip().lower() == order_side
        ):
            return True
    return False


def review_candidate(
    portfolio,
    candidate,
    campaign_date,
    date_from_iso,
    *,
    thresholds,
    cycle_goals=(20.0, 10.0, 5.0),
    support_stakes=None,
    first_loss_caps=None,
    later_loss_caps=None,
    min_price=25.0,
    max_price=60.0,
    max_open=3,
    daily_loss_cap=300.0,
    daily_loss_cap_pct=0.0,
    fee_rate=0.07,
    spread_min_tier="strong",
    goal_tolerance=0.05,
    partial_recovery_enabled=False,
    full_recovery_losses=2,
    partial_recovery_fraction=0.50,
    campaign_id="",
    bot_number=1,
    allowed_market_types=("moneyline", "spread"),
    spread_min_conservative_edge=0.0,
    mid_live_spread_min_conservative_edge=0.0,
    total_min_tier="qualified",
    total_min_conservative_edge=0.0,
    total_line_tolerance=0.01,
    total_min_book_families=2,
    total_min_sharp_books=1,
    pregame_enabled=False,
    fixed_stake=None,
    fixed_stake_metadata=None,
    allow_same_market_add_on=False,
):
    bot_number = campaign_bot_number({"bot_number": bot_number})
    support_stakes = support_stakes or {"qualified": 5.0, "strong": 10.0, "elite": 15.0}
    first_loss_caps = first_loss_caps or {"qualified": 300.0, "strong": 300.0, "elite": 300.0}
    later_loss_caps = later_loss_caps or {"qualified": 300.0, "strong": 300.0, "elite": 300.0}
    context = build_campaign_context(
        portfolio,
        campaign_date,
        date_from_iso,
        cycle_goals=cycle_goals,
        max_open=max_open,
        daily_loss_cap=daily_loss_cap,
        daily_loss_cap_pct=daily_loss_cap_pct,
        fee_rate=fee_rate,
        goal_tolerance=goal_tolerance,
        partial_recovery_enabled=partial_recovery_enabled,
        full_recovery_losses=full_recovery_losses,
        partial_recovery_fraction=partial_recovery_fraction,
        campaign_id=campaign_id,
        bot_number=bot_number,
    )
    review = {
        **context,
        "active": True,
        "eligible": False,
        "reason": "",
        "role": "",
        "quality_tier": "",
        "applied_stake": 0.0,
    }
    if context["complete"]:
        review["reason"] = "campaign_complete"
        return review
    if context["daily_loss_cap_hit"]:
        review["reason"] = "campaign_daily_loss_cap"
        return review
    is_pregame = bool(
        pregame_enabled
        and candidate.get("pregame_eligible")
        and not candidate.get("game_started")
    )
    if not candidate.get("game_started") and not is_pregame:
        review["reason"] = "campaign_live_only"
        return review
    if candidate.get("game_started") and not candidate.get("has_live_score_context"):
        review["reason"] = "campaign_live_score_required"
        return review
    if candidate.get("game_completed"):
        review["reason"] = "campaign_game_completed"
        return review
    market_type = str(candidate.get("market_type") or "").lower()
    allowed_market_types = {
        str(value or "").strip().lower()
        for value in (allowed_market_types or ())
        if str(value or "").strip()
    }
    if market_type not in allowed_market_types:
        review["reason"] = "campaign_market_type"
        return review
    price = float(candidate.get("entry_price") or 0)
    if price < float(min_price) or price > float(max_price):
        review["reason"] = "campaign_price_range"
        return review
    if context["bot_live_active_open_count"] >= int(max_open):
        review["reason"] = "campaign_open_slots_full"
        return review
    if _same_open_market(portfolio, candidate, bot_number) and not allow_same_market_add_on:
        review["reason"] = "campaign_open_market_duplicate"
        return review

    tier, metrics = quality_tier(candidate, thresholds)
    review["quality_metrics"] = metrics
    review["quality_thresholds"] = thresholds
    if not tier:
        review["reason"] = "campaign_quality_filter"
        return review
    review["quality_tier"] = tier
    tier_rank = {"qualified": 1, "strong": 2, "elite": 3}
    required_spread_rank = tier_rank.get(str(spread_min_tier or "strong").lower(), 2)
    if (
        market_type == "spread"
        and tier_rank.get(tier, 0) < required_spread_rank
    ):
        review["reason"] = "campaign_spread_requires_strong"
        return review
    if market_type == "spread":
        timing_bucket = _timing_bucket(candidate)
        required_conservative_edge = float(spread_min_conservative_edge or 0)
        if timing_bucket == "mid_live":
            required_conservative_edge = max(
                required_conservative_edge,
                float(mid_live_spread_min_conservative_edge or 0),
            )
        conservative_edge = _conservative_edge(candidate)
        review["spread_edge_guard"] = {
            "timing_bucket": timing_bucket,
            "conservative_edge": conservative_edge,
            "required_conservative_edge": required_conservative_edge,
        }
        if (
            required_conservative_edge > 0
            and (
                conservative_edge is None
                or conservative_edge + 1e-9 < required_conservative_edge
            )
        ):
            review["reason"] = "campaign_spread_conservative_edge"
            return review
    if market_type == "total":
        required_total_rank = tier_rank.get(str(total_min_tier or "qualified").lower(), 1)
        if tier_rank.get(tier, 0) < required_total_rank:
            review["reason"] = "campaign_total_requires_stronger_tier"
            return review
        total_review = _total_market_review(
            candidate,
            min_conservative_edge=total_min_conservative_edge,
            line_tolerance=total_line_tolerance,
            min_book_families=total_min_book_families,
            min_sharp_books=total_min_sharp_books,
        )
        review["total_market_guard"] = total_review
        if not total_review["ok"]:
            review["reason"] = total_review["failures"][0]
            return review

    if fixed_stake is not None:
        requested_stake = money(max(0.0, float(fixed_stake or 0)))
        price_fraction = price / 100.0
        required_contracts = max(
            0,
            int(math.floor(requested_stake / price_fraction + 1e-9)),
        )
        daily_contract_cap = maximum_contracts_for_caps(
            price,
            fee_rate,
            requested_stake,
            (
                context["daily_loss_remaining"]
                if context.get("daily_loss_cap_enabled")
                else float("inf")
            ),
        )
        planned_contracts = min(required_contracts, daily_contract_cap)
        stake = money(planned_contracts * price_fraction)
        estimated_fee = contract_fee(planned_contracts, price, fee_rate)
        planned_win_profit = contract_win_profit(planned_contracts, price, fee_rate)
        fully_funded = bool(
            required_contracts > 0
            and planned_contracts == required_contracts
        )
        metadata = dict(fixed_stake_metadata or {})
        review.update({
            "eligible": fully_funded,
            "reason": "eligible" if fully_funded else "campaign_full_size_unavailable",
            "role": "primary",
            "campaign_date": campaign_date,
            "cycle_number": context["cycle_number"],
            "cycle_goal": money(context["cycle_goal"]),
            "quality_tier": tier,
            "target_profit": planned_win_profit,
            "full_recovery_target_profit": money(context["target_remaining"]),
            "recovery_mode": "unit_flat",
            "sizing_mode": "confidence_units",
            "required_contracts": required_contracts,
            "planned_contracts": planned_contracts,
            "planned_target_met": fully_funded,
            "planned_win_profit": planned_win_profit,
            "estimated_fee": estimated_fee,
            "worst_case_cost": money(stake + estimated_fee),
            "raw_recovery_stake": requested_stake,
            "stake_cap": requested_stake,
            "daily_stake_cap": money(daily_contract_cap * price_fraction),
            "daily_loss_trimmed": planned_contracts < required_contracts,
            "applied_stake": stake,
            "entry_price": money(price),
            "max_single_stake": requested_stake,
            "fixed_stake_requested": requested_stake,
            "fixed_stake_metadata": metadata,
            "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        return review

    role = "support" if context["open_primary"] else "primary"

    cycle_goal = float(context["cycle_goal"])
    losses = int(context["cycle_loss_count"])
    price_fraction = price / 100.0
    full_target_profit = money(context["target_remaining"])
    target_profit = money(support_stakes[tier]) if role == "support" else full_target_profit
    recovery_mode = "support" if role == "support" else "full"
    if role == "primary" and context.get("partial_recovery_active"):
        drawdown = money(max(0.0, -float(context.get("cycle_realized_profit") or 0)))
        partial_target = money(
            float(context.get("cycle_goal") or 0)
            + drawdown * float(context.get("partial_recovery_fraction") or 0)
        )
        target_profit = money(min(full_target_profit, max(0.01, partial_target)))
        recovery_mode = "partial"
    required_contracts = contracts_for_target_profit(target_profit, price, fee_rate)
    required_stake = money(required_contracts * price_fraction)
    configured_limit = money(
        (first_loss_caps if losses <= 1 else later_loss_caps)[tier]
    )
    configured_cap = money(
        required_stake
        if role == "support" or configured_limit <= 0
        else configured_limit
    )
    configured_contract_cap = max(
        0,
        int(math.floor(configured_cap / price_fraction + 1e-9)),
    )
    daily_contract_cap = maximum_contracts_for_caps(
        price,
        fee_rate,
        configured_cap,
        (
            context["daily_loss_remaining"]
            if context.get("daily_loss_cap_enabled")
            else float("inf")
        ),
    )
    planned_contracts = min(required_contracts, configured_contract_cap, daily_contract_cap)
    stake = money(planned_contracts * price_fraction)
    estimated_fee = contract_fee(planned_contracts, price, fee_rate)
    planned_win_profit = contract_win_profit(planned_contracts, price, fee_rate)
    daily_stake_cap = money(daily_contract_cap * price_fraction)
    stake_cap = money(min(configured_cap, daily_stake_cap))
    fully_funded = bool(
        required_contracts > 0
        and planned_contracts == required_contracts
        and planned_win_profit + 1e-9 >= target_profit
    )

    review.update({
        "eligible": fully_funded,
        "reason": (
            "eligible"
            if fully_funded
            else "campaign_full_size_unavailable"
            if planned_contracts > 0
            else "campaign_invalid_stake"
        ),
        "role": role,
        "campaign_date": campaign_date,
        "cycle_number": context["cycle_number"],
        "cycle_goal": money(cycle_goal),
        "quality_tier": tier,
        "target_profit": money(target_profit),
        "full_recovery_target_profit": full_target_profit,
        "recovery_mode": recovery_mode,
        "required_contracts": required_contracts,
        "planned_contracts": planned_contracts,
        "planned_target_met": planned_win_profit + 1e-9 >= target_profit,
        "planned_win_profit": planned_win_profit,
        "estimated_fee": estimated_fee,
        "worst_case_cost": money(stake + estimated_fee),
        "raw_recovery_stake": required_stake,
        "stake_cap": money(stake_cap),
        "daily_stake_cap": daily_stake_cap,
        "daily_loss_trimmed": planned_contracts < required_contracts and daily_contract_cap <= configured_contract_cap,
        "applied_stake": money(stake),
        "entry_price": money(price),
        "max_single_stake": money(
            min(daily_loss_cap, max(max(first_loss_caps.values()), max(later_loss_caps.values())))
            if daily_loss_cap > 0 and max(max(first_loss_caps.values()), max(later_loss_caps.values())) > 0
            else max(max(first_loss_caps.values()), max(later_loss_caps.values()))
        ),
        "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    })
    return review
