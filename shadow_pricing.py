"""Fee- and correlation-aware pricing used for shadow evaluation only.

The live bots attach this analysis to candidates and reports, but execution code
does not consume it.  Keeping the calculator pure makes it safe to compare the
new policy with the current policy before any live promotion.
"""

from collections import Counter

from crypto_pricing import kalshi_order_fee


SHADOW_ALGORITHM_VERSION = "fee-correlation-v1"


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def estimated_taker_fee_per_contract(price_cents, fee_rate=0.07, fee_schedule=None):
    """Return the rounded per-contract fee for the current series schedule."""
    return kalshi_order_fee(
        price_cents,
        1.0,
        schedule=fee_schedule,
        liquidity_role="taker",
        fallback_rate=fee_rate,
    )["fee_per_contract_dollars"]


def kelly_metrics(
    probability_pct,
    price_cents,
    bankroll,
    kelly_fraction,
    max_stake_pct,
    max_stake_amount=None,
    fee_rate=0.07,
    fee_schedule=None,
    liquidity_role="taker",
    probability_low_pct=None,
):
    """Return raw and fee-aware edge/Kelly metrics for a binary contract."""
    probability = max(0.0, min(1.0, _number(probability_pct) / 100.0))
    sizing_probability = max(
        0.0,
        min(
            1.0,
            _number(
                probability_low_pct
                if probability_low_pct is not None
                else probability_pct
            ) / 100.0,
        ),
    )
    price = max(0.0, min(1.0, _number(price_cents) / 100.0))
    bankroll = max(0.0, _number(bankroll))
    fee_detail = kalshi_order_fee(
        price_cents,
        1.0,
        schedule=fee_schedule,
        liquidity_role=liquidity_role,
        fallback_rate=fee_rate,
    )
    fee = fee_detail["fee_per_contract_dollars"]
    fee_adjusted_cost = min(1.0, price + fee)

    raw_edge_pp = (probability - price) * 100.0
    net_edge_pp = (probability - fee_adjusted_cost) * 100.0
    raw_full_kelly = (probability - price) / (1.0 - price) if 0.0 < price < 1.0 else 0.0
    fee_full_kelly = (
        (sizing_probability - fee_adjusted_cost) / (1.0 - fee_adjusted_cost)
        if 0.0 < fee_adjusted_cost < 1.0
        else 0.0
    )
    raw_full_kelly = max(0.0, raw_full_kelly)
    fee_full_kelly = max(0.0, fee_full_kelly)

    cap = bankroll * max(0.0, _number(max_stake_pct))
    if max_stake_amount is not None and _number(max_stake_amount) > 0:
        cap = min(cap, _number(max_stake_amount))
    raw_stake = min(bankroll * raw_full_kelly * max(0.0, _number(kelly_fraction)), cap)
    fee_stake = min(bankroll * fee_full_kelly * max(0.0, _number(kelly_fraction)), cap)

    return {
        "probability_pct": round(probability * 100.0, 3),
        "sizing_probability_pct": round(sizing_probability * 100.0, 3),
        "entry_price_cents": round(price * 100.0, 3),
        "estimated_fee_per_contract": round(fee, 6),
        "estimated_fee_edge_pp": round(fee * 100.0, 3),
        "fee_adjusted_cost_cents": round(fee_adjusted_cost * 100.0, 3),
        "raw_edge_pp": round(raw_edge_pp, 3),
        "net_edge_pp": round(net_edge_pp, 3),
        "raw_full_kelly": round(raw_full_kelly, 6),
        "fee_aware_full_kelly": round(fee_full_kelly, 6),
        "raw_kelly_stake": round(max(0.0, raw_stake), 2),
        "fee_aware_kelly_stake": round(max(0.0, fee_stake), 2),
        "fee_schedule": fee_detail,
    }


def analyze_shadow_candidate(
    *,
    probability_pct,
    price_cents,
    bankroll,
    capital_base,
    open_positions,
    event_key,
    group_key,
    event_field,
    group_field,
    min_net_edge_pp,
    kelly_fraction,
    max_stake_pct,
    max_stake_amount=None,
    min_stake=0.0,
    fee_rate=0.07,
    fee_schedule=None,
    liquidity_role="taker",
    probability_low_pct=None,
    event_exposure_pct=0.03,
    group_exposure_pct=0.08,
    max_event_positions=1,
    current_filter_reasons=None,
):
    """Evaluate a candidate without altering any live decision or stake."""
    metrics = kelly_metrics(
        probability_pct,
        price_cents,
        bankroll,
        kelly_fraction,
        max_stake_pct,
        max_stake_amount=max_stake_amount,
        fee_rate=fee_rate,
        fee_schedule=fee_schedule,
        liquidity_role=liquidity_role,
        probability_low_pct=probability_low_pct,
    )
    open_rows = [row for row in (open_positions or []) if row.get("status") == "open"]
    event_rows = [row for row in open_rows if event_key and row.get(event_field) == event_key]
    group_rows = [row for row in open_rows if group_key and row.get(group_field) == group_key]
    event_exposure = sum(max(0.0, _number(row.get("stake"))) for row in event_rows)
    group_exposure = sum(max(0.0, _number(row.get("stake"))) for row in group_rows)
    capital_base = max(0.0, _number(capital_base))
    event_cap = capital_base * max(0.0, _number(event_exposure_pct))
    group_cap = capital_base * max(0.0, _number(group_exposure_pct))
    event_remaining = max(0.0, event_cap - event_exposure)
    group_remaining = max(0.0, group_cap - group_exposure)

    current_filter_reasons = list(dict.fromkeys(current_filter_reasons or []))
    reasons = []
    if current_filter_reasons:
        reasons.append("current_quality_filter")
    if metrics["net_edge_pp"] <= 0:
        reasons.append("fee_negative_ev")
    if metrics["net_edge_pp"] < _number(min_net_edge_pp):
        reasons.append("net_edge_below_threshold")
    if max_event_positions > 0 and len(event_rows) >= int(max_event_positions):
        reasons.append("correlated_event_position_cap")
    if event_cap > 0 and event_remaining <= 0:
        reasons.append("correlated_event_exposure_cap")
    if group_cap > 0 and group_remaining <= 0:
        reasons.append("correlated_group_exposure_cap")

    recommended = metrics["fee_aware_kelly_stake"]
    if event_cap > 0:
        recommended = min(recommended, event_remaining)
    if group_cap > 0:
        recommended = min(recommended, group_remaining)
    if reasons:
        recommended = 0.0
    minimum = max(0.0, _number(min_stake))
    if 0 < recommended < minimum:
        reasons.append("fee_adjusted_stake_below_minimum")
        recommended = 0.0

    recommended = round(max(0.0, recommended), 2)
    contract_cost = max(0.0, metrics["fee_adjusted_cost_cents"] / 100.0)
    return {
        "version": SHADOW_ALGORITHM_VERSION,
        "mode": "shadow_only",
        "affects_execution": False,
        **metrics,
        "min_net_edge_pp": round(_number(min_net_edge_pp), 3),
        "fee_threshold_pass": metrics["net_edge_pp"] >= _number(min_net_edge_pp),
        "current_filter_pass": not current_filter_reasons,
        "current_filter_reasons": current_filter_reasons,
        "event_key": str(event_key or ""),
        "group_key": str(group_key or ""),
        "same_event_open_count": len(event_rows),
        "event_open_exposure": round(event_exposure, 2),
        "event_exposure_cap": round(event_cap, 2),
        "event_exposure_remaining": round(event_remaining, 2),
        "group_open_exposure": round(group_exposure, 2),
        "group_exposure_cap": round(group_cap, 2),
        "group_exposure_remaining": round(group_remaining, 2),
        "fee_aware_correlated_stake": recommended,
        "estimated_contracts": round(recommended / contract_cost, 3) if contract_cost > 0 else 0.0,
        "decision": "shadow_approved" if recommended > 0 and not reasons else "shadow_skip",
        "reasons": list(dict.fromkeys(reasons)),
    }


def summarize_shadow_candidates(candidates, top_limit=25):
    rows = [row for row in (candidates or []) if isinstance(row.get("shadow"), dict)]
    shadows = [row["shadow"] for row in rows]
    reasons = Counter(reason for shadow in shadows for reason in shadow.get("reasons", []))
    approved = [row for row in rows if row["shadow"].get("decision") == "shadow_approved"]
    fee_positive = [row for row in rows if _number(row["shadow"].get("net_edge_pp")) > 0]
    threshold_pass = [row for row in rows if row["shadow"].get("fee_threshold_pass")]
    ranked = sorted(
        rows,
        key=lambda row: (
            _number(row["shadow"].get("fee_aware_correlated_stake")),
            _number(row["shadow"].get("net_edge_pp")),
        ),
        reverse=True,
    )
    avg_fee = sum(_number(row["shadow"].get("estimated_fee_edge_pp")) for row in rows) / max(1, len(rows))
    return {
        "version": SHADOW_ALGORITHM_VERSION,
        "mode": "shadow_only",
        "affects_execution": False,
        "candidate_count": len(rows),
        "raw_positive_ev_count": sum(1 for row in shadows if _number(row.get("raw_edge_pp")) > 0),
        "fee_positive_ev_count": len(fee_positive),
        "net_threshold_pass_count": len(threshold_pass),
        "correlation_approved_count": len(approved),
        "shadow_recommended_stake": round(sum(_number(row["shadow"].get("fee_aware_correlated_stake")) for row in approved), 2),
        "average_fee_drag_pp": round(avg_fee, 3),
        "reason_counts": [
            {"label": label, "count": count}
            for label, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        ],
        "top_opportunities": [
            {
                "ticker": row.get("ticker") or row.get("kalshi_ticker"),
                "event": row.get("event_ticker") or row.get("game_key"),
                "group": row.get("asset") or row.get("sport_key"),
                "label": row.get("title") or row.get("selected_team") or row.get("kalshi_title"),
                "side": row.get("side") or row.get("order_side"),
                "entry_price": row.get("entry_price"),
                "raw_edge_pp": row["shadow"].get("raw_edge_pp"),
                "net_edge_pp": row["shadow"].get("net_edge_pp"),
                "fee_drag_pp": row["shadow"].get("estimated_fee_edge_pp"),
                "fee_aware_kelly_stake": row["shadow"].get("fee_aware_kelly_stake"),
                "recommended_stake": row["shadow"].get("fee_aware_correlated_stake"),
                "same_event_open_count": row["shadow"].get("same_event_open_count"),
                "decision": row["shadow"].get("decision"),
                "reasons": row["shadow"].get("reasons", []),
                "current_filter_reasons": row["shadow"].get("current_filter_reasons", []),
            }
            for row in ranked[: max(0, int(top_limit))]
        ],
    }


def apply_final_quality_filters(candidates):
    """Overlay the bot's final quality decision onto pre-execution shadow math."""
    for candidate in candidates or []:
        shadow = candidate.get("shadow")
        if not isinstance(shadow, dict):
            continue
        final_reasons = list(dict.fromkeys(candidate.get("skip_reasons") or []))
        if not final_reasons:
            continue
        shadow["current_filter_pass"] = False
        shadow["current_filter_reasons"] = final_reasons
        if "current_quality_filter" not in shadow.get("reasons", []):
            shadow.setdefault("reasons", []).insert(0, "current_quality_filter")
        shadow["fee_aware_correlated_stake"] = 0.0
        shadow["estimated_contracts"] = 0.0
        shadow["decision"] = "shadow_skip"
    return candidates


def summarize_shadow_outcomes(rows):
    """Summarize realized results for bets that captured an at-entry shadow view."""
    tracked = [row for row in (rows or []) if isinstance(row.get("shadow"), dict)]
    settled = [row for row in tracked if row.get("status") == "settled" or row.get("result") in {"WIN", "LOSS", "VOID"}]

    def bucket(bucket_rows):
        stake = sum(max(0.0, _number(row.get("stake"))) for row in bucket_rows)
        profit = sum(_number(row.get("profit")) for row in bucket_rows)
        wins = sum(1 for row in bucket_rows if row.get("result") == "WIN")
        losses = sum(1 for row in bucket_rows if row.get("result") == "LOSS")
        return {
            "settled": len(bucket_rows),
            "wins": wins,
            "losses": losses,
            "stake": round(stake, 2),
            "profit": round(profit, 2),
            "roi_pct": round((profit / stake) * 100.0, 2) if stake > 0 else 0.0,
        }

    approved = [row for row in settled if row["shadow"].get("decision") == "shadow_approved"]
    filtered = [row for row in settled if row["shadow"].get("decision") != "shadow_approved"]
    return {
        "tracked_open": sum(1 for row in tracked if row not in settled),
        "tracked_settled": len(settled),
        "approved": bucket(approved),
        "filtered": bucket(filtered),
    }
