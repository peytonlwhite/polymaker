import math


def _number(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def quality_core_review(
    *,
    bankroll,
    metrics,
    minimums,
    elite_minimums,
    base_stake_pct,
    base_stake_cap,
    elite_multiplier,
    elite_stake_pct,
    elite_stake_cap,
    min_stake=0.0,
):
    """Return one quality decision and a bankroll-based base/elite stake.

    Daily targets, prior profit, drawdown, and loss streaks are intentionally
    absent. They may control whether trading pauses, but never the stake.
    """
    bankroll = max(0.0, _number(bankroll))
    normalized_metrics = {key: _number(value) for key, value in (metrics or {}).items()}
    normalized_minimums = {key: _number(value) for key, value in (minimums or {}).items()}
    normalized_elite = {key: _number(value) for key, value in (elite_minimums or {}).items()}

    failed = [
        key
        for key, threshold in normalized_minimums.items()
        if normalized_metrics.get(key, 0.0) < threshold
    ]
    eligible = bankroll > 0 and not failed
    elite = eligible and bool(normalized_elite) and all(
        normalized_metrics.get(key, 0.0) >= threshold
        for key, threshold in normalized_elite.items()
    )

    base_pct = max(0.0, _number(base_stake_pct))
    base_cap = max(0.0, _number(base_stake_cap))
    minimum_stake = max(0.0, _number(min_stake))
    base_stake = max(minimum_stake, bankroll * base_pct) if eligible else 0.0
    if base_cap > 0:
        base_stake = min(base_stake, base_cap)
    base_stake = min(base_stake, bankroll)

    applied_stake = base_stake
    if elite:
        elite_cap_pct = max(0.0, _number(elite_stake_pct))
        elite_cap = max(0.0, _number(elite_stake_cap))
        elite_limits = [bankroll]
        if elite_cap_pct > 0:
            elite_limits.append(bankroll * elite_cap_pct)
        if elite_cap > 0:
            elite_limits.append(elite_cap)
        applied_stake = min(
            max(base_stake, base_stake * max(1.0, _number(elite_multiplier, 1.0))),
            *elite_limits,
        )

    return {
        "active": eligible,
        "eligible": eligible,
        "tier": "elite" if elite else "base" if eligible else "rejected",
        "failed_metrics": failed,
        "metrics": {key: round(value, 4) for key, value in normalized_metrics.items()},
        "minimums": normalized_minimums,
        "elite_minimums": normalized_elite,
        "bankroll": round(bankroll, 2),
        "base_stake_pct": base_pct,
        "base_stake_cap": base_cap,
        "base_stake": round(base_stake, 2),
        "elite_multiplier": max(1.0, _number(elite_multiplier, 1.0)),
        "elite_stake_pct": max(0.0, _number(elite_stake_pct)),
        "elite_stake_cap": max(0.0, _number(elite_stake_cap)),
        "applied_stake": round(applied_stake, 2),
        "sizing_basis": "bankroll_and_quality_only",
    }
