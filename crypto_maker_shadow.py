"""Paper-only maker/taker execution experiment for short-horizon crypto.

Promotion evidence deliberately uses a strict-through fill model.  A public
trade at our hypothetical limit does not prove that an order behind the
existing queue would have filled; a trade strictly through the limit does.
"""

import math
import statistics
from datetime import datetime, timedelta, timezone

from crypto_pricing import kalshi_order_fee


VERSION = "crypto-maker-taker-shadow-v2"
LEGACY_FILL_MODEL = "trade_at_or_through_limit_v1"
STRICT_FILL_MODEL = "strict_trade_through_limit_v2"
ONE_SIDED_90_Z = 1.2815515655446004


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def empty_ledger():
    return {
        "version": VERSION,
        "mode": "shadow_only",
        "research_role": "execution_overlay_only",
        "updated_at": None,
        "records": [],
    }


def normalize_ledger(value):
    ledger = value if isinstance(value, dict) else {}
    ledger["version"] = VERSION
    ledger["mode"] = "shadow_only"
    ledger["research_role"] = "execution_overlay_only"
    ledger["records"] = ledger.get("records") if isinstance(ledger.get("records"), list) else []
    for row in ledger["records"]:
        if not isinstance(row, dict):
            continue
        if not row.get("fill_model"):
            row["fill_model"] = LEGACY_FILL_MODEL
        row["eligible_for_overlay_evaluation"] = bool(
            row.get("fill_model") == STRICT_FILL_MODEL
        )
        row["eligible_for_promotion"] = False
        row.setdefault("queue_position_modeled", False)
    return ledger


def update_candidates(
    ledger,
    candidates,
    *,
    now=None,
    minimum_minutes=6.0,
    maximum_minutes=11.0,
    minimum_spread_cents=2.0,
    max_records=5000,
):
    ledger = normalize_ledger(ledger)
    captured_at = _now(now).isoformat()
    by_id = {str(row.get("id") or ""): row for row in ledger["records"]}
    added = []
    newly_filled = []
    for candidate in candidates or []:
        if candidate.get("market_lane") != "crypto_15m":
            continue
        ticker = str(candidate.get("ticker") or "")
        side = str(candidate.get("side") or "").lower()
        if not ticker or side not in {"yes", "no"}:
            continue
        record_id = f"{ticker}:{side}"
        micro = candidate.get("kalshi_microstructure") or {}
        last_yes = micro.get("last_trade_price_cents")
        existing = by_id.get(record_id)
        if existing and existing.get("status") == "awaiting_fill" and last_yes is not None:
            maker_price = _number(existing.get("maker_price_cents"))
            trade_ts = _number(micro.get("last_trade_ts"))
            prior_trade_ts = _number(existing.get("last_observed_trade_ts"))
            trade_side_price = (
                _number(last_yes)
                if side == "yes"
                else 100.0 - _number(last_yes)
            )
            new_trade = trade_ts > prior_trade_ts
            fill_crossed = trade_side_price < maker_price - 1e-9
            if new_trade and abs(trade_side_price - maker_price) <= 1e-9:
                existing["equal_limit_trade_observations"] = int(
                    existing.get("equal_limit_trade_observations") or 0
                ) + 1
            if new_trade and fill_crossed:
                existing["status"] = "filled"
                existing["filled_at"] = captured_at
                existing["fill_evidence"] = "public_trade_strictly_through_limit"
                existing["fill_trade_yes_price_cents"] = _number(last_yes)
                existing["fill_trade_side_price_cents"] = round(trade_side_price, 4)
                newly_filled.append(existing)
            existing["last_observed_trade_ts"] = max(prior_trade_ts, trade_ts)
            continue
        if existing:
            continue
        minutes = _number(candidate.get("minutes_to_close"))
        if not minimum_minutes <= minutes <= maximum_minutes:
            continue
        bid = micro.get(f"best_{side}_bid_cents")
        ask = micro.get(f"best_{side}_entry_price_cents")
        if ask is None:
            ask = candidate.get("entry_price")
        if bid is None or ask is None:
            continue
        spread = _number(ask) - _number(bid)
        if spread < minimum_spread_cents:
            continue
        maker_price = _number(bid)
        probability_low = _number(candidate.get("selected_side_probability_low"))
        fee_schedule = candidate.get("fee_schedule") or {}
        maker_fee = kalshi_order_fee(
            maker_price,
            1.0,
            schedule=fee_schedule,
            liquidity_role="maker",
        )
        maker_fee_cents = _number(maker_fee.get("fee_per_contract_dollars")) * 100.0
        maker_edge_low = probability_low - maker_price - maker_fee_cents
        if maker_edge_low <= 0:
            continue
        row = {
            "id": record_id,
            "status": "awaiting_fill",
            "mode": "shadow_only",
            "fill_model": STRICT_FILL_MODEL,
            "eligible_for_overlay_evaluation": True,
            "eligible_for_promotion": False,
            "queue_position_modeled": False,
            "strict_through_required": True,
            "captured_at": captured_at,
            "ticker": ticker,
            "event_ticker": candidate.get("event_ticker"),
            "series_ticker": candidate.get("series_ticker"),
            "asset": candidate.get("asset"),
            "side": side,
            "close_time": candidate.get("close_time"),
            "minutes_to_close": minutes,
            "maker_price_cents": round(maker_price, 4),
            "taker_price_cents": round(_number(ask), 4),
            "spread_cents": round(spread, 4),
            "probability_low": round(probability_low, 4),
            "maker_edge_low": round(maker_edge_low, 4),
            "taker_edge_low": candidate.get("edge_low"),
            "maker_fee_cents": round(maker_fee_cents, 4),
            "taker_fee_cents": candidate.get("exact_fee_cents"),
            "maker_queue_ahead_contracts": round(
                _number(micro.get(f"top1_{side}_depth_contracts")),
                4,
            ),
            "equal_limit_trade_observations": 0,
            "last_observed_trade_ts": _number(micro.get("last_trade_ts")),
        }
        ledger["records"].append(row)
        by_id[record_id] = row
        added.append(row)
    if max_records > 0 and len(ledger["records"]) > int(max_records):
        ledger["records"] = ledger["records"][-int(max_records):]
    ledger["updated_at"] = captured_at
    return {"added": added, "filled": newly_filled}


def settle_records(ledger, fetch_market, *, now=None, grace_minutes=2.0, max_checks=20):
    ledger = normalize_ledger(ledger)
    current = _now(now)
    settled = []
    checks = 0
    market_cache = {}
    for row in ledger["records"]:
        if row.get("status") not in {"awaiting_fill", "filled"} or checks >= int(max_checks):
            continue
        try:
            if current < _now(row.get("close_time")) + timedelta(minutes=max(0.0, grace_minutes)):
                continue
        except (TypeError, ValueError):
            continue
        ticker = str(row.get("ticker") or "")
        if ticker not in market_cache:
            checks += 1
            try:
                market_cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                market_cache[ticker] = {"_error": f"{type(exc).__name__}: {exc}"[:300]}
        market = market_cache[ticker]
        result = str(market.get("result") or "").lower()
        if str(market.get("status") or "").lower() != "finalized" or result not in {"yes", "no"}:
            continue
        if row.get("status") == "awaiting_fill":
            row.update({"status": "expired_unfilled", "settled_at": current.isoformat(), "market_result": result})
            settled.append(row)
            continue
        won = str(row.get("side")) == result
        maker_cost = _number(row.get("maker_price_cents")) / 100.0 + _number(row.get("maker_fee_cents")) / 100.0
        taker_cost = _number(row.get("taker_price_cents")) / 100.0 + _number(row.get("taker_fee_cents")) / 100.0
        row.update({
            "status": "settled",
            "settled_at": current.isoformat(),
            "settlement_source": "kalshi_finalized_market",
            "market_result": result,
            "result": "WIN" if won else "LOSS",
            "maker_profit_per_contract": round((1.0 if won else 0.0) - maker_cost, 6),
            "taker_profit_per_contract": round((1.0 if won else 0.0) - taker_cost, 6),
        })
        settled.append(row)
    ledger["updated_at"] = current.isoformat()
    return settled


def _independent_records(records):
    by_event = {}
    for row in sorted(records, key=lambda value: str(value.get("captured_at") or "")):
        event = str(row.get("event_ticker") or row.get("ticker") or row.get("id") or "")
        if event and event not in by_event:
            by_event[event] = row
    return list(by_event.values())


def _lower_confidence_bound(values):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    return statistics.fmean(values) - (
        ONE_SIDED_90_Z * statistics.stdev(values) / math.sqrt(len(values))
    )


def _profit_summary(records):
    settled = [row for row in records if row.get("status") == "settled"]
    maker_profit = sum(_number(row.get("maker_profit_per_contract")) for row in settled)
    taker_profit = sum(_number(row.get("taker_profit_per_contract")) for row in settled)
    maker_cost = sum(
        _number(row.get("maker_price_cents")) / 100.0
        + _number(row.get("maker_fee_cents")) / 100.0
        for row in settled
    )
    taker_cost = sum(
        _number(row.get("taker_price_cents")) / 100.0
        + _number(row.get("taker_fee_cents")) / 100.0
        for row in settled
    )
    return {
        "tracked": len(records),
        "settled": len(settled),
        "maker_profit": maker_profit,
        "taker_profit": taker_profit,
        "maker_cost": maker_cost,
        "taker_cost": taker_cost,
        "maker_roi": 100.0 * maker_profit / maker_cost if maker_cost else 0.0,
        "taker_roi": 100.0 * taker_profit / taker_cost if taker_cost else 0.0,
    }


def summarize(
    ledger,
    *,
    minimum_independent_markets=100,
    adverse_fill_reserve_cents=1.0,
    minimum_reserved_roi_pct=10.0,
    upstream_signal_validated=False,
):
    ledger = normalize_ledger(ledger)
    records = ledger["records"]
    eligible = _independent_records([
        row
        for row in records
        if row.get("fill_model") == STRICT_FILL_MODEL
        and row.get("eligible_for_overlay_evaluation")
    ])
    legacy = _independent_records([
        row for row in records if row.get("fill_model") != STRICT_FILL_MODEL
    ])
    filled = [row for row in eligible if row.get("status") in {"filled", "settled"}]
    settled = [row for row in eligible if row.get("status") == "settled"]
    maker_profit = sum(_number(row.get("maker_profit_per_contract")) for row in settled)
    taker_profit = sum(_number(row.get("taker_profit_per_contract")) for row in settled)
    reserve = max(0.0, _number(adverse_fill_reserve_cents)) / 100.0
    reserved_values = [
        _number(row.get("maker_profit_per_contract")) - reserve
        for row in settled
    ]
    reserved_profit = sum(reserved_values)
    reserved_cost = sum(
        _number(row.get("maker_price_cents")) / 100.0
        + _number(row.get("maker_fee_cents")) / 100.0
        + reserve
        for row in settled
    )
    reserved_roi = 100.0 * reserved_profit / reserved_cost if reserved_cost else 0.0
    lower_bound = _lower_confidence_bound(reserved_values)
    midpoint = len(settled) // 2
    halves = []
    for rows in (settled[:midpoint], settled[midpoint:]):
        profit = sum(
            _number(row.get("maker_profit_per_contract")) - reserve
            for row in rows
        )
        halves.append({
            "settled": len(rows),
            "reserved_profit": round(profit, 6),
            "profitable": bool(rows and profit > 0),
        })
    strict_evidence = bool(
        settled
        and all(
            row.get("fill_evidence") == "public_trade_strictly_through_limit"
            and row.get("fill_model") == STRICT_FILL_MODEL
            for row in settled
        )
    )
    gates = {
        "minimum_independent_markets": {
            "passed": len(settled) >= max(1, int(minimum_independent_markets)),
            "actual": len(settled),
            "required": max(1, int(minimum_independent_markets)),
        },
        "positive_reserved_profit": {
            "passed": reserved_profit > 0,
            "actual": round(reserved_profit, 6),
            "required": "> 0",
        },
        "minimum_reserved_roi": {
            "passed": reserved_roi >= _number(minimum_reserved_roi_pct),
            "actual": round(reserved_roi, 4),
            "required": _number(minimum_reserved_roi_pct),
        },
        "positive_one_sided_90_lower_bound": {
            "passed": lower_bound is not None and lower_bound > 0,
            "actual": round(lower_bound, 6) if lower_bound is not None else None,
            "required": "> 0",
        },
        "both_chronological_halves_profitable": {
            "passed": all(row["profitable"] for row in halves),
            "first_half": halves[0],
            "second_half": halves[1],
        },
        "strict_fill_evidence": {
            "passed": strict_evidence,
            "fill_model": STRICT_FILL_MODEL,
            "queue_position_modeled": False,
        },
        "independent_upstream_signal_validated": {
            "passed": bool(upstream_signal_validated),
            "required": True,
            "reason": "maker execution is not an independent trading signal",
        },
    }
    overlay_quality_gates = {
        key: row
        for key, row in gates.items()
        if key != "independent_upstream_signal_validated"
    }
    overlay_quality_qualified = bool(
        overlay_quality_gates
        and all(row.get("passed") for row in overlay_quality_gates.values())
    )
    qualified = bool(overlay_quality_qualified and upstream_signal_validated)
    legacy_summary = _profit_summary(legacy)
    return {
        "version": VERSION,
        "mode": "shadow_only",
        "affects_execution": False,
        "automatic_promotion": False,
        "status": (
            "PASS"
            if qualified
            else "COLLECTING"
            if len(settled) < max(1, int(minimum_independent_markets))
            else "FAIL"
        ),
        "research_role": "execution_overlay_only",
        "recommendation_only": True,
        "independent_signal": False,
        "promotion_eligible": False,
        "overlay_quality_qualified": overlay_quality_qualified,
        "activation_eligible": qualified,
        "fill_model": STRICT_FILL_MODEL,
        "tracked": len(records),
        "overlay_evaluation_tracked": len(eligible),
        "promotion_eligible_tracked": 0,
        "independent_markets": len(settled),
        "awaiting_fill": sum(row.get("status") == "awaiting_fill" for row in eligible),
        "filled": len(filled),
        "fill_rate": round(len(filled) / len(eligible), 4) if eligible else 0.0,
        "settled": len(settled),
        "expired_unfilled": sum(row.get("status") == "expired_unfilled" for row in eligible),
        "maker_profit_per_contract": round(maker_profit, 6),
        "matched_taker_profit_per_contract": round(taker_profit, 6),
        "maker_incremental_profit": round(maker_profit - taker_profit, 6),
        "adverse_fill_reserve_cents": round(reserve * 100.0, 4),
        "reserved_maker_profit": round(reserved_profit, 6),
        "reserved_maker_roi": round(reserved_roi, 4),
        "one_sided_90_lower_bound_profit": (
            round(lower_bound, 6) if lower_bound is not None else None
        ),
        "chronological_halves": halves,
        "qualification_gates": gates,
        "legacy_non_promotable": {
            "fill_model": LEGACY_FILL_MODEL,
            "reason": "equal-price trades did not establish queue-position fill",
            **{
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in legacy_summary.items()
            },
        },
        "recent_records": sorted(records, key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""), reverse=True)[:100],
        "updated_at": ledger.get("updated_at"),
    }
