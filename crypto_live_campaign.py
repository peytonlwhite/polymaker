from __future__ import annotations

import math
from datetime import datetime

from crypto_pricing import kalshi_order_fee


OWNER = "crypto_15m_campaign"
FIELD = "crypto_live_campaign"
ROLES = {"primary", "support"}


def campaign_bot_number(row, default=1):
    """Return the persistent campaign lane; legacy rows belong to Bot 1."""
    campaign = (row or {}).get(FIELD) or {}
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


def _is_open(row):
    return str(row.get("status") or "").lower() in {"open", "pending", "resting"}


def _is_settled(row):
    return str(row.get("status") or "").lower() == "settled"


def _row_date(row, date_from_iso):
    campaign = row.get(FIELD) or {}
    return str(campaign.get("campaign_date") or date_from_iso(row.get("placed_at") or row.get("created_at")))


def _campaign_rows(portfolio, campaign_date, date_from_iso, campaign_id="", bot_number=1):
    bot_number = campaign_bot_number({"bot_number": bot_number})
    return [
        row
        for row in [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]
        if row.get("strategy_owner") == OWNER
        and (row.get(FIELD) or {}).get("role") in ROLES
        and campaign_bot_number(row) == bot_number
        and _row_date(row, date_from_iso) == campaign_date
        and (
            not campaign_id
            or str((row.get(FIELD) or {}).get("campaign_id") or "") == str(campaign_id)
        )
    ]


def _is_live_bot_position(row):
    if not _is_open(row) or str(row.get("mode") or "").lower() != "live":
        return False
    return str(row.get("strategy_owner") or "") not in {"", "user_bet", "manual_live_import"}


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


def _daily_bot_rows(
    portfolio,
    campaign_date,
    date_from_iso,
    bot_number=1,
    include_settled=None,
):
    bot_number = campaign_bot_number({"bot_number": bot_number})
    rows = []
    for row in [*(portfolio.get("history") or []), *(portfolio.get("bets") or [])]:
        owner = str(row.get("strategy_owner") or "")
        if owner in {"", "user_bet", "manual_live_import"}:
            continue
        if owner == OWNER and campaign_bot_number(row) != bot_number:
            continue
        if _is_open(row):
            rows.append(row)
            continue
        settled_date = date_from_iso(row.get("settled_at") or row.get("placed_at") or row.get("created_at"))
        if (
            _is_settled(row)
            and settled_date == campaign_date
            and (
                include_settled is None
                or include_settled(
                    row.get("settled_at")
                    or row.get("placed_at")
                    or row.get("created_at")
                )
            )
        ):
            rows.append(row)
    return rows


def build_campaign_context(
    portfolio,
    campaign_date,
    date_from_iso,
    *,
    cycle_goals=(5.0, 2.5, 1.25),
    max_open=1,
    daily_loss_cap=300.0,
    daily_loss_cap_pct=0.0,
    fee_rate=0.07,
    goal_tolerance=0.05,
    partial_recovery_enabled=False,
    full_recovery_losses=2,
    partial_recovery_fraction=0.50,
    campaign_id="",
    bot_number=1,
    include_daily_settled=None,
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
    primary_settled = sorted(
        [
            row
            for row in settled
            if (row.get(FIELD) or {}).get("role") == "primary"
            and str(row.get("result") or "").upper() in {"WIN", "LOSS"}
        ],
        key=lambda row: str(row.get("settled_at") or row.get("placed_at") or ""),
    )
    primary_wins = sum(1 for row in primary_settled if str(row.get("result") or "").upper() == "WIN")
    consecutive_primary_losses = 0
    for row in reversed(primary_settled):
        if str(row.get("result") or "").upper() != "LOSS":
            break
        consecutive_primary_losses += 1

    realized_profit = money(sum(money(row.get("profit")) for row in settled))
    goal_tolerance = max(0.0, money(goal_tolerance))
    # Replay settlements in order. One settlement can complete at most one
    # cycle, and any overshoot becomes the next cycle's baseline rather than
    # silently completing additional cycles.
    ordered_settled = sorted(
        settled,
        key=lambda item: str(item.get("settled_at") or item.get("placed_at") or ""),
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
    cycle_realized_profit = money(realized_profit - cycle_start_profit)
    cycle_target = 0.0 if complete else money(cycle_start_profit + cycle_goal)
    target_remaining = (
        0.0
        if complete
        else money(max(0.0, cycle_goal - cycle_realized_profit))
    )
    cycle_primary_rows = [
        row
        for row in current_cycle_rows
        if (row.get(FIELD) or {}).get("role") == "primary"
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
    all_open = portfolio.get("bets") or []
    bot_live_open = [
        row
        for row in all_open
        if _is_live_bot_position(row)
        and row.get("strategy_owner") == OWNER
        and campaign_bot_number(row) == bot_number
    ]
    open_primary = any(
        _is_open(row)
        and row.get("strategy_owner") == OWNER
        and (row.get(FIELD) or {}).get("role") == "primary"
        and campaign_bot_number(row) == bot_number
        for row in all_open
    )
    daily_rows = _daily_bot_rows(
        portfolio,
        campaign_date,
        date_from_iso,
        bot_number,
        include_settled=include_daily_settled,
    )
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
    percentage_daily_loss_cap = money(money(portfolio.get("balance")) * daily_loss_cap_pct)
    daily_loss_cap = (
        percentage_daily_loss_cap
        if percentage_daily_loss_cap > 0
        else configured_daily_loss_cap
    )
    daily_loss_remaining = money(max(0.0, daily_loss_cap + bot_daily_realized_profit - bot_daily_open_risk))
    daily_loss_cap_hit = daily_loss_cap > 0 and daily_loss_remaining <= 0
    status = "complete" if complete else "daily_loss_cap" if daily_loss_cap_hit else "active"
    return {
        "enabled": True,
        "active": not complete,
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
        "progression_mode": "separate_cycle_profit_targets",
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
        "settled_count": len(settled),
        "campaign_open_count": sum(1 for row in rows if _is_open(row)),
        "bot_live_open_count": len(bot_live_open),
        "max_open": int(max_open),
        "open_primary": open_primary,
        "daily_loss_cap": daily_loss_cap,
        "configured_daily_loss_cap": configured_daily_loss_cap,
        "daily_loss_cap_pct": round(daily_loss_cap_pct, 6),
        "daily_loss_cap_source": "bankroll_pct" if percentage_daily_loss_cap > 0 else "configured_amount",
        "daily_loss_cap_hit": daily_loss_cap_hit,
        "bot_daily_realized_profit": bot_daily_realized_profit,
        "bot_daily_open_risk": bot_daily_open_risk,
        "daily_loss_remaining": daily_loss_remaining,
    }


def _same_asset_window_open(portfolio, candidate, bot_number=1):
    bot_number = campaign_bot_number({"bot_number": bot_number})
    event_ticker = str(candidate.get("event_ticker") or "")
    asset = str(candidate.get("asset") or "")
    close_time = str(candidate.get("close_time") or "")
    for row in portfolio.get("bets") or []:
        if not _is_live_bot_position(row):
            continue
        if row.get("strategy_owner") != OWNER or campaign_bot_number(row) != bot_number:
            continue
        if event_ticker and str(row.get("event_ticker") or "") == event_ticker:
            return True
        if asset and close_time and str(row.get("asset") or "") == asset and str(row.get("close_time") or "") == close_time:
            return True
    return False


def _exact_contract_open_on_other_bot(portfolio, candidate, bot_number=1):
    """Prevent isolated campaign lanes from trading against each other."""
    bot_number = campaign_bot_number({"bot_number": bot_number})
    ticker = str(candidate.get("ticker") or "")
    if not ticker:
        return False
    for row in portfolio.get("bets") or []:
        if not _is_live_bot_position(row):
            continue
        if row.get("strategy_owner") != OWNER:
            continue
        if campaign_bot_number(row) == bot_number:
            continue
        if str(row.get("ticker") or "") == ticker:
            return True
    return False


def _same_expiry_open_count(portfolio, candidate):
    """Count live campaign positions sharing this 15-minute settlement window."""
    close_time = str(candidate.get("close_time") or "")
    if not close_time:
        return 0
    return sum(
        1
        for row in portfolio.get("bets") or []
        if _is_live_bot_position(row)
        and row.get("strategy_owner") == OWNER
        and str(row.get("close_time") or "") == close_time
    )


def review_candidate(
    portfolio,
    candidate,
    campaign_date,
    date_from_iso,
    *,
    cycle_goals=(5.0, 2.5, 1.25),
    support_stakes=(1.25, 2.5, 3.75),
    min_edge=10.0,
    min_confidence=65.0,
    longshot_max_price=20.0,
    longshot_min_edge=15.0,
    longshot_min_confidence=75.0,
    strong_edge=14.0,
    strong_confidence=72.0,
    elite_edge=18.0,
    elite_confidence=78.0,
    min_price=10.0,
    max_price=80.0,
    min_minutes_remaining=2.0,
    max_minutes_remaining=12.0,
    mid_price_min=45.0,
    mid_price_max=54.0,
    mid_price_min_edge=6.0,
    mid_price_min_confidence=65.0,
    early_entry_min_minutes=10.0,
    early_entry_min_edge=6.0,
    early_entry_min_confidence=70.0,
    normal_max_same_expiry_open=1,
    absolute_max_same_expiry_open=2,
    same_expiry_elite_min_edge=6.0,
    same_expiry_elite_min_confidence=70.0,
    max_open=1,
    daily_loss_cap=300.0,
    daily_loss_cap_pct=0.0,
    fee_rate=0.07,
    goal_tolerance=0.05,
    partial_recovery_enabled=False,
    full_recovery_losses=4,
    partial_recovery_fraction=0.50,
    campaign_id="",
    bot_number=1,
    primary_target_profit_cap=None,
    primary_recovery_mode_override="",
    include_daily_settled=None,
    fixed_stake=None,
    fixed_stake_metadata=None,
    daily_loss_remaining_override=None,
    cycle_completion_blocks=True,
):
    bot_number = campaign_bot_number({"bot_number": bot_number})
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
        include_daily_settled=include_daily_settled,
    )
    if daily_loss_remaining_override is not None:
        context["daily_loss_remaining"] = money(
            max(0.0, float(daily_loss_remaining_override or 0))
        )
        context["daily_loss_cap_hit"] = bool(
            float(context.get("daily_loss_cap") or 0) > 0
            and context["daily_loss_remaining"] <= 0
        )
        context["daily_loss_remaining_source"] = "campaign_lane_context"
    review = {
        **context,
        "eligible": False,
        "reason": "",
        "role": "",
        "quality_tier": "",
        "applied_stake": 0.0,
        "observed": {
            "price_cents": round(float(candidate.get("entry_price") or 0), 4),
            "edge": round(float(candidate.get("edge") or 0), 4),
            "confidence": round(float(candidate.get("confidence") or 0), 4),
            "minutes_remaining": round(float(candidate.get("minutes_to_close") or 0), 4),
        },
        "requirements": {
            "minimum_price_cents": float(min_price),
            "maximum_price_cents": float(max_price),
            "minimum_edge": float(min_edge),
            "minimum_confidence": float(min_confidence),
            "minimum_minutes_remaining": float(min_minutes_remaining),
            "maximum_minutes_remaining": float(max_minutes_remaining),
            "market_kind": "above",
        },
        "failed_checks": [],
    }
    if context["complete"] and cycle_completion_blocks:
        review["reason"] = "campaign_complete"
        return review
    if context["daily_loss_cap_hit"]:
        review["reason"] = "campaign_daily_loss_cap"
        return review
    if not bool(candidate.get("is_15m_market")):
        review["reason"] = "campaign_15m_only"
        return review
    minutes = float(candidate.get("minutes_to_close") or 0)
    if minutes < float(min_minutes_remaining) or minutes > float(max_minutes_remaining):
        review["reason"] = "campaign_entry_window"
        review["failed_checks"] = [
            {
                "metric": "minutes_remaining",
                "actual": round(minutes, 4),
                "minimum": float(min_minutes_remaining),
                "maximum": float(max_minutes_remaining),
            }
        ]
        return review
    if str(candidate.get("market_kind") or "").lower() != "above":
        review["reason"] = "campaign_target_market_only"
        review["failed_checks"] = [{
            "metric": "market_kind",
            "actual": str(candidate.get("market_kind") or "unknown"),
            "required": "above",
        }]
        return review
    price = float(candidate.get("entry_price") or 0)
    if price < float(min_price) or price > float(max_price):
        review["reason"] = "campaign_price_range"
        review["failed_checks"] = [
            {
                "metric": "price_cents",
                "actual": round(price, 4),
                "minimum": float(min_price),
                "maximum": float(max_price),
            }
        ]
        if float(candidate.get("edge") or 0) < float(min_edge):
            review["failed_checks"].append({
                "metric": "edge",
                "actual": round(float(candidate.get("edge") or 0), 4),
                "minimum": float(min_edge),
            })
        if float(candidate.get("confidence") or 0) < float(min_confidence):
            review["failed_checks"].append({
                "metric": "confidence",
                "actual": round(float(candidate.get("confidence") or 0), 4),
                "minimum": float(min_confidence),
            })
        return review
    if context["bot_live_open_count"] >= int(max_open):
        review["reason"] = "campaign_open_slots_full"
        return review
    if _exact_contract_open_on_other_bot(portfolio, candidate, bot_number):
        review["reason"] = "campaign_exact_contract_conflict"
        return review
    if _same_asset_window_open(portfolio, candidate, bot_number):
        review["reason"] = "campaign_asset_window_duplicate"
        return review

    edge = float(candidate.get("edge") or 0)
    confidence = float(candidate.get("confidence") or 0)
    if edge < float(min_edge) or confidence < float(min_confidence):
        review["reason"] = "campaign_quality_filter"
        review["failed_checks"] = []
        if edge < float(min_edge):
            review["failed_checks"].append({
                "metric": "edge", "actual": round(edge, 4), "minimum": float(min_edge),
            })
        if confidence < float(min_confidence):
            review["failed_checks"].append({
                "metric": "confidence", "actual": round(confidence, 4), "minimum": float(min_confidence),
            })
        return review
    if (
        float(mid_price_min) <= price <= float(mid_price_max)
        and (
            edge < float(mid_price_min_edge)
            or confidence < float(mid_price_min_confidence)
        )
    ):
        review["reason"] = "campaign_mid_price_quality_filter"
        review["failed_checks"] = []
        if edge < float(mid_price_min_edge):
            review["failed_checks"].append({"metric": "edge", "actual": round(edge, 4), "minimum": float(mid_price_min_edge), "scope": "mid_price"})
        if confidence < float(mid_price_min_confidence):
            review["failed_checks"].append({"metric": "confidence", "actual": round(confidence, 4), "minimum": float(mid_price_min_confidence), "scope": "mid_price"})
        return review
    if (
        minutes > float(early_entry_min_minutes)
        and (
            edge < float(early_entry_min_edge)
            or confidence < float(early_entry_min_confidence)
        )
    ):
        review["reason"] = "campaign_early_entry_quality_filter"
        review["failed_checks"] = []
        if edge < float(early_entry_min_edge):
            review["failed_checks"].append({"metric": "edge", "actual": round(edge, 4), "minimum": float(early_entry_min_edge), "scope": "early_entry"})
        if confidence < float(early_entry_min_confidence):
            review["failed_checks"].append({"metric": "confidence", "actual": round(confidence, 4), "minimum": float(early_entry_min_confidence), "scope": "early_entry"})
        return review
    if (
        price <= float(longshot_max_price)
        and (
            edge < float(longshot_min_edge)
            or confidence < float(longshot_min_confidence)
        )
    ):
        review["reason"] = "campaign_longshot_quality_filter"
        review["failed_checks"] = []
        if edge < float(longshot_min_edge):
            review["failed_checks"].append({"metric": "edge", "actual": round(edge, 4), "minimum": float(longshot_min_edge), "scope": "longshot"})
        if confidence < float(longshot_min_confidence):
            review["failed_checks"].append({"metric": "confidence", "actual": round(confidence, 4), "minimum": float(longshot_min_confidence), "scope": "longshot"})
        return review
    same_expiry_open_count = _same_expiry_open_count(portfolio, candidate)
    normal_same_expiry_limit = max(0, int(normal_max_same_expiry_open or 0))
    absolute_same_expiry_limit = max(
        normal_same_expiry_limit,
        int(absolute_max_same_expiry_open or normal_same_expiry_limit),
    )
    review["same_expiry_open_count"] = same_expiry_open_count
    review["same_expiry_normal_limit"] = normal_same_expiry_limit
    review["same_expiry_absolute_limit"] = absolute_same_expiry_limit
    if same_expiry_open_count >= absolute_same_expiry_limit:
        review["reason"] = "campaign_expiry_window_concentration"
        return review
    if (
        same_expiry_open_count >= normal_same_expiry_limit
        and (
            edge < float(same_expiry_elite_min_edge)
            or confidence < float(same_expiry_elite_min_confidence)
        )
    ):
        review["reason"] = "campaign_expiry_window_requires_elite"
        return review
    review["same_expiry_elite_exception"] = bool(
        same_expiry_open_count >= normal_same_expiry_limit
    )

    if fixed_stake is not None:
        requested_stake = money(max(0.0, float(fixed_stake or 0)))
        price_fraction = price / 100.0
        metadata = dict(fixed_stake_metadata or {})
        exact_fee_per_contract = (
            max(0.0, float(candidate.get("exact_fee_cents") or 0)) / 100.0
            if "exact_fee_cents" in candidate
            else float(fee_rate) * price_fraction * (1.0 - price_fraction)
        )
        fee_schedule = candidate.get("fee_schedule") or {}

        def fixed_order_fee(contracts):
            contracts = max(0, int(contracts or 0))
            if fee_schedule.get("authoritative"):
                return float(kalshi_order_fee(
                    price,
                    contracts,
                    schedule=fee_schedule,
                    liquidity_role="taker",
                    fallback_rate=fee_rate,
                ).get("fee_dollars") or 0)
            return exact_fee_per_contract * contracts

        requested_contracts = max(
            0,
            int(math.floor(requested_stake / max(0.0001, price_fraction) + 1e-12)),
        )
        minimum_unit_stake = max(0.0, float(metadata.get("unit_size") or 0))
        minimum_unit_contracts = (
            max(
                1,
                int(
                    math.floor(
                        minimum_unit_stake / max(0.0001, price_fraction) + 1e-12
                    )
                ),
            )
            if minimum_unit_stake > 0
            else 0
        )
        daily_loss_remaining = max(0.0, float(context["daily_loss_remaining"]))
        maximum_contracts = max(
            0,
            int(math.floor(daily_loss_remaining / max(0.0001, price_fraction) + 1e-12)),
        )
        while (
            maximum_contracts > 0
            and maximum_contracts * price_fraction + fixed_order_fee(maximum_contracts)
            > daily_loss_remaining + 1e-9
        ):
            maximum_contracts -= 1
        full_unit_capacity = bool(
            minimum_unit_contracts <= 0
            or maximum_contracts >= minimum_unit_contracts
        )
        applied_contracts = (
            min(requested_contracts, maximum_contracts)
            if full_unit_capacity
            else 0
        )
        stake = money(applied_contracts * price_fraction)
        applied_fee = fixed_order_fee(applied_contracts)
        capacity_limited = bool(
            requested_contracts > 0
            and minimum_unit_contracts > 0
            and not full_unit_capacity
        )
        review.update({
            "active": True,
            "status": "active",
            "complete": False,
            "cycle_tracking_only": True,
            "cycle_was_complete": bool(context.get("complete")),
            "eligible": stake > 0,
            "reason": (
                "eligible"
                if stake > 0
                else "campaign_unit_capacity"
                if capacity_limited
                else "campaign_invalid_stake"
            ),
            "role": "primary",
            "quality_tier": str(metadata.get("quality_tier") or "unit"),
            "quality_metrics": {"edge": edge, "confidence": confidence},
            "target_profit": money(
                applied_contracts * (1.0 - price_fraction) - applied_fee
            ),
            "full_recovery_target_profit": money(context.get("target_remaining")),
            "recovery_mode": "unit_flat",
            "sizing_mode": "probability_edge_kelly_units",
            "estimated_fee": money(applied_fee),
            "fee_per_contract": round(
                applied_fee / applied_contracts if applied_contracts else 0.0,
                6,
            ),
            "fee_source": (
                "candidate_series_schedule"
                if "exact_fee_cents" in candidate
                else "quadratic_fallback"
            ),
            "worst_case_cost": money(stake + applied_fee),
            "raw_recovery_stake": requested_stake,
            "required_contracts": requested_contracts,
            "applied_contracts": applied_contracts,
            "daily_stake_cap": money(maximum_contracts * price_fraction),
            "daily_loss_trimmed": applied_contracts < requested_contracts,
            "minimum_unit_stake": money(minimum_unit_stake),
            "minimum_unit_contracts": minimum_unit_contracts,
            "full_unit_capacity": full_unit_capacity,
            "applied_stake": stake,
            "entry_price": money(price),
            "fixed_stake_requested": requested_stake,
            "fixed_stake_metadata": metadata,
            "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        return review

    if edge >= float(elite_edge) and confidence >= float(elite_confidence):
        tier = "elite"
        support_stake = support_stakes[2]
    elif edge >= float(strong_edge) and confidence >= float(strong_confidence):
        tier = "strong"
        support_stake = support_stakes[1]
    else:
        tier = "qualified"
        support_stake = support_stakes[0]

    role = "support" if context["open_primary"] else "primary"
    cycle_goal = float(context["cycle_goal"])
    price_fraction = price / 100.0
    exact_fee_per_contract = (
        max(0.0, float(candidate.get("exact_fee_cents") or 0)) / 100.0
        if "exact_fee_cents" in candidate
        else float(fee_rate) * price_fraction * (1.0 - price_fraction)
    )
    fee_schedule = candidate.get("fee_schedule") or {}

    def exact_order_fee(contracts):
        contracts = max(0, int(contracts or 0))
        if fee_schedule.get("authoritative"):
            return float(kalshi_order_fee(
                price,
                contracts,
                schedule=fee_schedule,
                liquidity_role="taker",
                fallback_rate=fee_rate,
            ).get("fee_dollars") or 0)
        return exact_fee_per_contract * contracts
    fee_per_staked_dollar = exact_fee_per_contract / price_fraction
    profit_per_dollar = max(
        0.0001,
        (1.0 - price_fraction - exact_fee_per_contract) / price_fraction,
    )
    full_target_profit = money(context["target_remaining"])
    target_profit = money(support_stake) if role == "support" else full_target_profit
    recovery_mode = "support" if role == "support" else "full"
    if role == "primary" and context.get("partial_recovery_active"):
        drawdown = money(max(0.0, -float(context.get("cycle_realized_profit") or 0)))
        partial_target = money(
            float(context.get("cycle_goal") or 0)
            + drawdown * float(context.get("partial_recovery_fraction") or 0)
        )
        target_profit = money(min(full_target_profit, max(0.01, partial_target)))
        recovery_mode = "partial"
    if role == "primary" and primary_target_profit_cap is not None:
        target_profit = money(
            min(
                target_profit,
                max(0.01, float(primary_target_profit_cap)),
            )
        )
        if target_profit + 0.009 < full_target_profit:
            recovery_mode = (
                str(primary_recovery_mode_override or "").strip()
                or "capped"
            )
    raw_stake_exact = target_profit / profit_per_dollar
    raw_stake = money(raw_stake_exact)
    loss_cost_per_dollar = 1.0 + fee_per_staked_dollar
    daily_stake_cap = money(float(context["daily_loss_remaining"]) / loss_cost_per_dollar)
    # Kalshi orders must use whole contracts. Rounding the dollar stake down in
    # the order layer could leave a winning bet short of the campaign target.
    # Round the required size up here, then trim only if the daily-loss ceiling
    # cannot fund that many contracts.
    required_contracts = max(1, int(math.ceil((raw_stake_exact / price_fraction) - 1e-12)))
    while (
        required_contracts * (1.0 - price_fraction)
        - exact_order_fee(required_contracts)
        + 1e-9
        < target_profit
    ):
        required_contracts += 1
    daily_loss_remaining = max(0.0, float(context["daily_loss_remaining"]))
    max_contracts = max(0, int(math.floor((daily_stake_cap / price_fraction) + 1e-12)))
    while (
        max_contracts > 0
        and max_contracts * price_fraction + exact_order_fee(max_contracts)
        > daily_loss_remaining + 1e-9
    ):
        max_contracts -= 1
    applied_contracts = min(required_contracts, max_contracts)
    stake = money(applied_contracts * price_fraction)
    required_stake = money(required_contracts * price_fraction)
    applied_fee = exact_order_fee(applied_contracts)

    review.update(
        {
            "eligible": stake > 0,
            "reason": "eligible" if stake > 0 else "campaign_invalid_stake",
            "role": role,
            "quality_tier": tier,
            "quality_metrics": {"edge": edge, "confidence": confidence},
            "cycle_goal": money(cycle_goal),
            "target_profit": money(target_profit),
            "full_recovery_target_profit": full_target_profit,
            "recovery_mode": recovery_mode,
            "profit_per_staked_dollar_after_fee": round(profit_per_dollar, 6),
            "estimated_fee": money(applied_fee),
            "fee_per_contract": round(
                applied_fee / applied_contracts if applied_contracts else 0.0,
                6,
            ),
            "fee_source": (
                "candidate_series_schedule"
                if "exact_fee_cents" in candidate
                else "quadratic_fallback"
            ),
            "worst_case_cost": money(stake + applied_fee),
            "raw_recovery_stake": money(raw_stake),
            "required_contracts": required_contracts,
            "applied_contracts": applied_contracts,
            "daily_stake_cap": daily_stake_cap,
            "daily_loss_trimmed": applied_contracts < required_contracts,
            "applied_stake": money(stake),
            "entry_price": money(price),
            "reviewed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
    )
    return review
