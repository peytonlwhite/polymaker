"""Fail-closed live pilot for the spot-lead plus cross-venue-flow signal.

This module deliberately owns qualification and sizing for the pilot.  It does
not use the legacy model edge, confidence, unit, cycle, or recovery paths.
"""

from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from crypto_pricing import kalshi_order_fee
from crypto_execution_safety import shard_cash
from crypto_signal_tournament_shadow import (
    CONTEMPORANEOUS_SPOT_FLOW_LANE,
    common_quality_review,
    entry_price,
    signal_reviews,
)


VERSION = "crypto-spot-flow-live-pilot-v2"
OWNER = "crypto_spot_flow_live_pilot"
CONFIRMED_TIER = "spot_flow_confirmed"
SPOT_ONLY_TIER = "spot_lead_only"
CHICAGO = ZoneInfo("America/Chicago")
ONE_SIDED_90_Z = 1.2815515655446004


def _bool(settings, key, default=False):
    value = (settings or {}).get(key, default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _parse_time(value):
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def configuration(settings=None):
    settings = settings or {}
    return {
        "enabled": _bool(settings, "CRYPTO_SPOT_FLOW_LIVE_PILOT_ENABLED"),
        "registered_at": settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_REGISTERED_AT"),
        "assets": tuple(
            part.strip().upper()
            for part in str(
                settings.get(
                    "CRYPTO_SPOT_FLOW_LIVE_PILOT_ASSETS",
                    "BTC,ETH,SOL,DOGE,XRP",
                )
            ).split(",")
            if part.strip()
        ),
        "minimum_bankroll": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_BANKROLL"), 0
        ),
        "sizing_bankroll_cap": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_SIZING_BANKROLL_CAP"),
            5000,
        ),
        "stake_fraction": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_STAKE_PCT"), 0.30
        )
        / 100.0,
        "maximum_contracts": max(
            1,
            _integer(
                settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_CONTRACTS"), 5
            ),
        ),
        "spot_only_enabled": _bool(
            settings,
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_ENABLED",
            False,
        ),
        "spot_only_minimum_price_cents": _number(
            settings.get(
                "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MIN_PRICE_CENTS"
            ),
            35,
        ),
        "spot_only_maximum_price_cents": _number(
            settings.get(
                "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MAX_PRICE_CENTS"
            ),
            80,
        ),
        "spot_only_minimum_minutes": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MIN_MINUTES"),
            5,
        ),
        "spot_only_maximum_minutes": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MAX_MINUTES"),
            13,
        ),
        "spot_only_minimum_strength_bps": _number(
            settings.get(
                "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MIN_STRENGTH_BPS"
            ),
            10,
        ),
        "spot_only_maximum_contracts": max(
            1,
            _integer(
                settings.get(
                    "CRYPTO_SPOT_FLOW_LIVE_PILOT_SPOT_ONLY_MAX_CONTRACTS"
                ),
                3,
            ),
        ),
        "minimum_price_cents": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_PRICE_CENTS"), 20
        ),
        "maximum_price_cents": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_PRICE_CENTS"), 80
        ),
        "minimum_minutes": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_MINUTES"), 2
        ),
        "maximum_minutes": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_MINUTES"), 13
        ),
        "maximum_adverse_move_cents": _number(
            settings.get(
                "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_ADVERSE_MOVE_CENTS", 1
            ),
        ),
        "maximum_reconciliation_age_seconds": _number(
            settings.get(
                "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_RECONCILIATION_AGE_SECONDS",
                90,
            ),
        ),
        "daily_entry_cap": max(
            1,
            _integer(
                settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_DAILY_ENTRY_CAP"), 25
            ),
        ),
        "daily_loss_fraction": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_DAILY_LOSS_PCT"), 0.90
        )
        / 100.0,
        "open_exposure_fraction": _number(
            settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_OPEN_EXPOSURE_PCT"),
            0.90,
        )
        / 100.0,
        "maximum_open_positions": max(
            1,
            _integer(
                settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_OPEN"), 4
            ),
        ),
        "maximum_open_per_expiry": max(
            1,
            _integer(
                settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_OPEN_PER_EXPIRY"),
                2,
            ),
        ),
        "live_review_settled_cap": max(
            1,
            _integer(
                settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_REVIEW_SETTLED_CAP"),
                100,
            ),
        ),
        "require_tournament_gate": _bool(
            settings,
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_TOURNAMENT_GATE",
            True,
        ),
        "require_forward_gate": _bool(
            settings,
            "CRYPTO_SPOT_FLOW_LIVE_PILOT_REQUIRE_FORWARD_GATE",
            True,
        ),
        "minimum_forward_markets": max(
            1,
            _integer(
                settings.get(
                    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_FORWARD_MARKETS", 100
                )
            ),
        ),
        "minimum_forward_days": max(
            1,
            _integer(
                settings.get("CRYPTO_SPOT_FLOW_LIVE_PILOT_MIN_FORWARD_DAYS"), 3
            ),
        ),
        "maximum_pair_capture_delta_seconds": max(
            0.0,
            _number(
                settings.get(
                    "CRYPTO_SPOT_FLOW_LIVE_PILOT_MAX_PAIR_DELTA_SECONDS"
                ),
                2.0,
            ),
        ),
    }


def is_pilot_candidate(candidate):
    return bool((candidate.get("spot_flow_live_pilot") or {}).get("active"))


def exact_signal_review(candidate, settings=None):
    config = configuration(settings)
    reasons = []
    quality = common_quality_review(candidate, settings)
    reasons.extend(quality.get("reasons") or [])
    if str(candidate.get("asset") or "").upper() not in config["assets"]:
        reasons.append("pilot_asset_not_enabled")
    signals = signal_reviews(candidate)
    spot = signals.get("spot_leads_kalshi") or {}
    flow = signals.get("cross_venue_flow_follow") or {}
    side = str(spot.get("side") or "").lower()
    tier = None
    maximum_contracts = config["maximum_contracts"]
    if not spot:
        reasons.append("spot_lead_signal_absent")
        if not flow:
            reasons.append("cross_venue_flow_signal_absent")
    elif flow:
        if side != str(flow.get("side") or "").lower():
            # An independently registered flow signal in the opposite
            # direction remains a hard veto for both live tiers.
            reasons.append("spot_flow_direction_mismatch")
        else:
            tier = CONFIRMED_TIER
    elif not config["spot_only_enabled"]:
        reasons.append("cross_venue_flow_signal_absent")
    else:
        tier = SPOT_ONLY_TIER
        maximum_contracts = min(
            config["maximum_contracts"],
            config["spot_only_maximum_contracts"],
        )
    price = entry_price(candidate, side) if side in {"yes", "no"} else -1.0
    # Price is a meaningful independent rejection only after a signal has
    # selected a tradable side.  Counting an undefined side as an out-of-band
    # price makes the funnel overstate the price gate whenever the spot signal
    # is absent.
    if side in {"yes", "no"} and not (
        config["minimum_price_cents"] <= price <= config["maximum_price_cents"]
    ):
        reasons.append("pilot_price_outside_band")
    elif tier == SPOT_ONLY_TIER and not (
        config["spot_only_minimum_price_cents"]
        <= price
        <= config["spot_only_maximum_price_cents"]
    ):
        reasons.append("pilot_spot_only_price_outside_band")
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    if tier == CONFIRMED_TIER and not (
        config["minimum_minutes"] <= minutes <= config["maximum_minutes"]
    ):
        reasons.append("pilot_confirmed_time_window")
    if tier == SPOT_ONLY_TIER:
        if not (
            config["spot_only_minimum_minutes"]
            <= minutes
            <= config["spot_only_maximum_minutes"]
        ):
            reasons.append("pilot_spot_only_time_window")
        if _number(spot.get("strength")) < config[
            "spot_only_minimum_strength_bps"
        ]:
            reasons.append("pilot_spot_only_strength_below_floor")
    fee_schedule = candidate.get("fee_schedule") or {}
    if not fee_schedule.get("authoritative"):
        reasons.append("fee_schedule_not_authoritative")
    return {
        "ok": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "tier": tier,
        "maximum_contracts": maximum_contracts,
        "side": side or None,
        "entry_price_cents": round(price, 6) if price >= 0 else None,
        "spot_strength_bps": spot.get("strength"),
        "flow_strength": flow.get("strength"),
        "spot_evidence": spot.get("evidence") or {},
        "flow_evidence": flow.get("evidence") or {},
        "quality": quality,
    }


def _cluster_lower(values, confidence_z=ONE_SIDED_90_Z):
    values = [float(value) for value in values]
    if not values:
        return {"clusters": 0, "mean": None, "standard_error": None, "lower_bound": None}
    mean = sum(values) / len(values)
    if len(values) < 2:
        standard_error = None
        lower = None
    else:
        variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
        standard_error = math.sqrt(variance / len(values))
        lower = mean - confidence_z * standard_error
    return {
        "clusters": len(values),
        "mean": round(mean, 6),
        "standard_error": round(standard_error, 6) if standard_error is not None else None,
        "lower_bound": round(lower, 6) if lower is not None else None,
        "confidence_level": 0.90,
    }


def prospective_summary(signal_ledger, settings=None):
    """Evaluate only same-side spot+flow records captured after registration."""
    config = configuration(settings)
    registered_at = _parse_time(config.get("registered_at"))
    records = (signal_ledger or {}).get("records") or []
    direct_by_ticker = {}
    by_ticker = defaultdict(dict)
    for row in records:
        captured_at = _parse_time(row.get("captured_at"))
        if not registered_at or not captured_at or captured_at < registered_at:
            continue
        lane = str(row.get("lane") or "")
        ticker = str(row.get("ticker") or "")
        if lane == CONTEMPORANEOUS_SPOT_FLOW_LANE:
            direct_by_ticker[ticker] = row
            continue
        if lane not in {"spot_leads_kalshi", "cross_venue_flow_follow"}:
            continue
        by_ticker[ticker][lane] = row

    def settled_result(row, *, source, capture_delta_seconds=0.0):
        if not row or row.get("status") != "settled":
            return None
        primary = row.get("primary") or {}
        comparator = row.get("comparator") or {}
        profit = primary.get("virtual_profit")
        comparator_profit = comparator.get("virtual_profit")
        if profit is None or comparator_profit is None:
            return None
        return {
            "ticker": str(row.get("ticker") or ""),
            "captured_at": row.get("captured_at"),
            "expiry_key": row.get("expiry_key") or row.get("close_time"),
            "profit": _number(profit),
            "comparator_profit": _number(comparator_profit),
            "delta": _number(profit) - _number(comparator_profit),
            "cost": _number(primary.get("total_cost_dollars")),
            "pairing_source": source,
            "capture_delta_seconds": round(capture_delta_seconds, 6),
        }

    paired = []
    direct_same_scan_settled = 0
    legacy_same_scan_pairs = 0
    noncontemporaneous_rejected = 0
    # New captures have one dedicated record produced from the exact candidate
    # snapshot used by execution qualification.  Prefer it over reconstructed
    # legacy lane pairs so one market can never enter the ledger twice.
    for ticker, row in direct_by_ticker.items():
        result = settled_result(row, source="direct_same_scan_record")
        if result:
            result["ticker"] = ticker
            paired.append(result)
            direct_same_scan_settled += 1

    for ticker, lanes in by_ticker.items():
        if ticker in direct_by_ticker:
            continue
        spot = lanes.get("spot_leads_kalshi")
        flow = lanes.get("cross_venue_flow_follow")
        if not spot or not flow:
            continue
        if (spot.get("primary") or {}).get("side") != (flow.get("primary") or {}).get("side"):
            continue
        spot_captured = _parse_time(spot.get("captured_at"))
        flow_captured = _parse_time(flow.get("captured_at"))
        if not spot_captured or not flow_captured:
            continue
        capture_delta = abs((spot_captured - flow_captured).total_seconds())
        spot_batch = str(spot.get("capture_batch_id") or "")
        flow_batch = str(flow.get("capture_batch_id") or "")
        same_batch = bool(spot_batch and flow_batch and spot_batch == flow_batch)
        if not same_batch and capture_delta > config["maximum_pair_capture_delta_seconds"]:
            noncontemporaneous_rejected += 1
            continue
        result = settled_result(
            spot,
            source="legacy_time_matched_pair",
            capture_delta_seconds=capture_delta,
        )
        if result:
            result["ticker"] = ticker
            paired.append(result)
            legacy_same_scan_pairs += 1
    paired.sort(key=lambda row: (str(row.get("captured_at") or ""), row["ticker"]))
    profit = sum(row["profit"] for row in paired)
    cost = sum(row["cost"] for row in paired)
    half = len(paired) // 2
    first = paired[:half]
    second = paired[half:]
    days = defaultdict(lambda: {"profit": 0.0, "delta": 0.0})
    expiries = defaultdict(lambda: {"profit": 0.0, "delta": 0.0})
    for row in paired:
        captured = _parse_time(row.get("captured_at"))
        day_key = captured.astimezone(CHICAGO).date().isoformat() if captured else "unknown"
        days[day_key]["profit"] += row["profit"]
        days[day_key]["delta"] += row["delta"]
        expiry_key = str(row.get("expiry_key") or row["ticker"])
        expiries[expiry_key]["profit"] += row["profit"]
        expiries[expiry_key]["delta"] += row["delta"]
    expiry_profit = _cluster_lower([value["profit"] for value in expiries.values()])
    expiry_delta = _cluster_lower([value["delta"] for value in expiries.values()])
    day_profit = _cluster_lower([value["profit"] for value in days.values()])
    gates = {
        "registration_present": registered_at is not None,
        "minimum_unique_markets": len(paired) >= config["minimum_forward_markets"],
        "minimum_observation_days": len(days) >= config["minimum_forward_days"],
        "profitable": profit > 0,
        "both_chronological_halves_profitable": bool(first and second)
        and sum(row["profit"] for row in first) > 0
        and sum(row["profit"] for row in second) > 0,
        "expiry_cluster_lower_positive": _number(expiry_profit.get("lower_bound"), -999) > 0,
        "paired_delta_expiry_lower_positive": _number(expiry_delta.get("lower_bound"), -999) > 0,
        "day_cluster_lower_positive": _number(day_profit.get("lower_bound"), -999) > 0,
    }
    return {
        "registered_at": config.get("registered_at"),
        "settled": len(paired),
        "observation_days": len(days),
        "profit": round(profit, 6),
        "cost": round(cost, 6),
        "roi": round(profit / cost * 100.0, 2) if cost > 0 else 0.0,
        "profit_delta": round(sum(row["delta"] for row in paired), 6),
        "halves": {
            "first_profit": round(sum(row["profit"] for row in first), 6),
            "second_profit": round(sum(row["profit"] for row in second), 6),
        },
        "expiry_cluster_inference": expiry_profit,
        "delta_expiry_cluster_inference": expiry_delta,
        "day_cluster_inference": day_profit,
        "pairing_diagnostics": {
            "direct_same_scan_settled": direct_same_scan_settled,
            "legacy_same_scan_pairs": legacy_same_scan_pairs,
            "noncontemporaneous_rejected": noncontemporaneous_rejected,
            "maximum_pair_capture_delta_seconds": config[
                "maximum_pair_capture_delta_seconds"
            ],
        },
        "gates": gates,
        "eligible": bool(gates and all(gates.values())),
    }


def validation_review(signal_summary, signal_ledger, settings=None):
    config = configuration(settings)
    spot_lane = next(
        (
            row
            for row in (signal_summary or {}).get("lanes") or []
            if row.get("lane") == "spot_leads_kalshi"
        ),
        {},
    )
    tournament_ok = bool((spot_lane.get("interim_review") or {}).get("eligible"))
    forward = prospective_summary(signal_ledger, settings)
    gates = {
        "pilot_enabled": config["enabled"],
        "spot_tournament_interim": tournament_ok,
        "prospective_combo_forward": bool(forward.get("eligible")),
    }
    bypassed = [name for name, required in (
        ("spot_tournament_interim", config["require_tournament_gate"]),
        ("prospective_combo_forward", config["require_forward_gate"]),
    ) if not required]
    return {
        "eligible": all(gates.values()) and not bypassed,
        "statistically_qualified": tournament_ok and bool(forward.get("eligible")),
        "bypassed_gates": bypassed,
        "gate_status": {key: "bypassed" if key in bypassed else "passed" if value else "failed" for key, value in gates.items()},
        "gates": gates,
        "spot_tournament": spot_lane,
        "prospective_combo": forward,
    }


def verified_bankroll(reconciliation, config=None, now=None):
    config = config or configuration()
    account = (reconciliation or {}).get("account") or {}
    generated_at = _parse_time((reconciliation or {}).get("generated_at"))
    current = now or datetime.now(timezone.utc)
    age = (current - generated_at).total_seconds() if generated_at else None
    reasons = []
    cash = _number(account.get("cash_balance"), -1.0)
    balance_raw = account.get("balance_raw") or {}
    portfolio_value = account.get("portfolio_value_dollars")
    if portfolio_value is None:
        portfolio_value = balance_raw.get("portfolio_value_dollars")
    if portfolio_value is None and balance_raw.get("portfolio_value") is not None:
        portfolio_value = _number(balance_raw.get("portfolio_value")) / 100.0
    portfolio_value = max(0.0, _number(portfolio_value))
    account_equity = max(0.0, cash) + portfolio_value
    if account.get("ok") is not True:
        reasons.append("pilot_account_unavailable")
    if account.get("positions_complete") is not True:
        reasons.append("pilot_positions_incomplete")
    if account.get("orders_complete") is not True:
        reasons.append("pilot_orders_incomplete")
    if cash < 0:
        reasons.append("pilot_cash_balance_unavailable")
    if age is None or age < -0.25 or age > config["maximum_reconciliation_age_seconds"]:
        reasons.append("pilot_reconciliation_stale")
    if (reconciliation or {}).get("unmatched_local_tickers"):
        reasons.append("pilot_unmatched_local_positions")
    if (reconciliation or {}).get("unmatched_remote_tickers"):
        reasons.append("pilot_unmatched_remote_positions")
    if account_equity < config["minimum_bankroll"]:
        reasons.append("pilot_bankroll_below_minimum")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "cash_balance": round(max(0.0, cash), 2),
        "portfolio_value": round(portfolio_value, 2),
        "account_equity": round(account_equity, 2),
        "sizing_bankroll": round(
            min(account_equity, config["sizing_bankroll_cap"]), 2
        ),
        "sizing_basis": "verified_account_equity",
        "reconciliation_age_seconds": round(age, 3) if age is not None else None,
    }


def _owner(row):
    return str(row.get("strategy_owner") or "")


def _live_rows(portfolio):
    return [
        row
        for row in (portfolio or {}).get("bets") or []
        if row.get("mode") == "live" and _owner(row) == OWNER
    ]


def risk_review(portfolio, reconciliation, validation, settings=None, now=None):
    config = configuration(settings)
    current = now or datetime.now(timezone.utc)
    bankroll = verified_bankroll(reconciliation, config, current)
    rows = _live_rows(portfolio)
    today = current.astimezone(CHICAGO).date()
    today_rows = []
    for row in rows:
        timestamp = _parse_time(
            row.get("placed_at") or row.get("created_at") or row.get("timestamp")
        )
        if timestamp and timestamp.astimezone(CHICAGO).date() == today:
            today_rows.append(row)
    settled_today = [row for row in today_rows if row.get("status") == "settled"]
    open_rows = [row for row in rows if row.get("status") == "open"]
    realized_profit = sum(_number(row.get("profit")) for row in settled_today)
    open_exposure = sum(_number(row.get("stake")) + _number(row.get("fee")) for row in open_rows)
    daily_loss_cap = bankroll["sizing_bankroll"] * config["daily_loss_fraction"]
    open_exposure_cap = bankroll["sizing_bankroll"] * config["open_exposure_fraction"]
    settled_total = sum(row.get("status") == "settled" for row in rows)
    reasons = list(bankroll["reasons"])
    if not config["require_tournament_gate"] or not config["require_forward_gate"]:
        reasons.append("pilot_validation_gate_bypassed")
    pending = ((reconciliation or {}).get("account") or {}).get("resting_orders") or []
    if any(str(row.get("ticker") or "").startswith(("KXBTC", "KXETH", "KXSOL", "KXDOGE", "KXXRP")) for row in pending):
        reasons.append("pilot_crypto_resting_orders_present")
    if not validation.get("eligible"):
        reasons.append("pilot_validation_not_ready")
    if len(today_rows) >= config["daily_entry_cap"]:
        reasons.append("pilot_daily_entry_cap")
    if max(0.0, -realized_profit) >= daily_loss_cap > 0:
        reasons.append("pilot_daily_loss_cap")
    if open_exposure >= open_exposure_cap > 0:
        reasons.append("pilot_open_exposure_cap")
    if len(open_rows) >= config["maximum_open_positions"]:
        reasons.append("pilot_max_open_positions")
    if settled_total >= config["live_review_settled_cap"]:
        reasons.append("pilot_live_review_due")
    return {
        "ok": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "bankroll": bankroll,
        "daily_entries": len(today_rows),
        "daily_entry_cap": config["daily_entry_cap"],
        "daily_realized_profit": round(realized_profit, 2),
        "daily_loss_cap": round(daily_loss_cap, 2),
        "open_positions": len(open_rows),
        "open_exposure": round(open_exposure, 2),
        "open_exposure_cap": round(open_exposure_cap, 2),
        "live_settled": settled_total,
        "live_review_settled_cap": config["live_review_settled_cap"],
    }


def size_for_bankroll(
    candidate,
    bankroll,
    settings=None,
    available_cash=None,
    maximum_contracts=None,
):
    config = configuration(settings)
    price = _number(candidate.get("entry_price"))
    schedule = candidate.get("fee_schedule") or {}
    target = max(0.0, _number(bankroll)) * config["stake_fraction"]
    one_fee = _number(
        kalshi_order_fee(price, 1, schedule=schedule, liquidity_role="taker").get(
            "fee_dollars"
        )
    )
    total_per_contract = price / 100.0 + one_fee
    contracts = (
        int(math.floor((target + 1e-9) / total_per_contract))
        if total_per_contract > 0
        else 0
    )
    contract_cap = min(
        config["maximum_contracts"],
        max(
            0,
            _integer(
                maximum_contracts,
                config["maximum_contracts"],
            ),
        ),
    )
    contracts = min(contract_cap, max(0, contracts))
    equity_sized_contracts = contracts
    cash_contract_cap = None
    if available_cash is not None:
        cash_contract_cap = (
            int(math.floor((max(0.0, _number(available_cash)) + 1e-9) / total_per_contract))
            if total_per_contract > 0
            else 0
        )
        contracts = min(contracts, max(0, cash_contract_cap))
    principal = round(contracts * price / 100.0, 2)
    fees = _number(
        kalshi_order_fee(
            price,
            contracts,
            schedule=schedule,
            liquidity_role="taker",
        ).get("fee_dollars")
    ) if contracts else 0.0
    return {
        "target_total_cost": round(target, 2),
        "contracts": contracts,
        "principal_stake": principal,
        "expected_fee_dollars": round(fees, 4),
        "expected_total_cost": round(principal + fees, 4),
        "bankroll": round(_number(bankroll), 2),
        "available_cash": (
            round(max(0.0, _number(available_cash)), 2)
            if available_cash is not None
            else None
        ),
        "cash_contract_cap": cash_contract_cap,
        "cash_limited": cash_contract_cap is not None
        and cash_contract_cap < equity_sized_contracts,
        "stake_fraction_pct": round(config["stake_fraction"] * 100.0, 4),
        "maximum_contracts": contract_cap,
    }


def build_execution_candidates(
    candidates,
    portfolio,
    reconciliation,
    signal_summary,
    signal_ledger,
    settings=None,
    now=None,
    precomputed_validation=None,
):
    config = configuration(settings)
    signal_matched_at = (_parse_time(now) or datetime.now(timezone.utc)).isoformat()
    validation = (
        precomputed_validation
        if isinstance(precomputed_validation, dict) and precomputed_validation
        else validation_review(signal_summary, signal_ledger, settings)
    )
    base_risk = risk_review(
        portfolio, reconciliation, validation, settings=settings, now=now
    )
    clones = []
    funnel = defaultdict(int)
    for candidate in candidates or []:
        funnel["reviewed"] += 1
        signal = exact_signal_review(candidate, settings)
        if not signal["ok"]:
            for reason in signal["reasons"]:
                funnel[f"reason:{reason}"] += 1
            continue
        funnel["signal_matched"] += 1
        funnel[f"signal_matched:{signal['tier']}"] += 1
        clone = deepcopy(candidate)
        clone["side"] = signal["side"]
        clone["entry_price"] = signal["entry_price_cents"]
        clone["strategy_owner"] = OWNER
        # Do not inherit campaign ownership, unit sizing, or recovery state from
        # the scanned candidate.  The pilot has one isolated sizing authority.
        for key in (
            "crypto_live_campaign",
            "crypto_units",
            "shared_recovery",
            "phase_two",
            "phase_two_attempt_id",
            "phase_two_cycle",
            "small_edge",
            "selective_edge",
            "live_core",
            "bot_number",
            "live_quote_refresh",
            "live_underlying_refresh",
            "live_slippage_reconfirmation",
            "live_unit_depth_downshift",
            "execution_quote_origin",
        ):
            clone.pop(key, None)
        clone["recovery_context"] = {
            "active": False,
            "reason": "disabled_for_spot_flow_live_pilot",
        }
        size = size_for_bankroll(
            clone,
            base_risk["bankroll"]["sizing_bankroll"],
            settings,
            available_cash=shard_cash((reconciliation or {}).get("account") or {}, clone.get("exchange_index")) or 0.0,
            maximum_contracts=signal["maximum_contracts"],
        )
        reasons = list(base_risk["reasons"])
        local_cash = shard_cash((reconciliation or {}).get("account") or {}, clone.get("exchange_index"))
        if local_cash is None:
            reasons.append("pilot_exchange_shard_balance_unavailable")
        elif local_cash <= 0:
            reasons.append("pilot_exchange_shard_cash_insufficient")
        clone["exchange_cash_available"] = local_cash
        if size["contracts"] < 1:
            reasons.append("pilot_stake_below_one_contract")
        ticker = str(clone.get("ticker") or "")
        open_rows = [row for row in _live_rows(portfolio) if row.get("status") == "open"]
        if any(str(row.get("ticker") or row.get("kalshi_ticker") or "") == ticker for row in open_rows):
            reasons.append("pilot_duplicate_ticker")
        expiry = str(clone.get("close_time") or clone.get("event_ticker") or "")
        expiry_open = sum(
            str(row.get("close_time") or row.get("event_ticker") or "") == expiry
            for row in open_rows
        )
        if expiry_open >= config["maximum_open_per_expiry"]:
            reasons.append("pilot_expiry_position_cap")
        remaining_exposure = max(
            0.0,
            base_risk["open_exposure_cap"] - base_risk["open_exposure"],
        )
        if size["principal_stake"] > remaining_exposure + 0.005:
            reasons.append("pilot_open_exposure_room_insufficient")
        review = {
            "active": True,
            "version": VERSION,
            "owner": OWNER,
            "tier": signal["tier"],
            "eligible": not reasons,
            "reason": reasons[0] if reasons else "eligible",
            "reasons": list(dict.fromkeys(reasons)),
            "signal": signal,
            "validation": validation,
            "risk": base_risk,
            "sizing": size,
            "recovery_enabled": False,
            "time_in_force": "fill_or_kill",
            "maximum_adverse_move_cents": config["maximum_adverse_move_cents"],
            "signal_matched_at": signal_matched_at,
        }
        clone["spot_flow_live_pilot"] = review
        clone["skip_reasons"] = list(review["reasons"])
        clone["decision"] = "eligible" if review["eligible"] else "skipped"
        clones.append(clone)
        funnel["eligible" if review["eligible"] else f"blocked:{review['reason']}"] += 1
        if review["eligible"]:
            funnel[f"eligible:{signal['tier']}"] += 1
    return clones, {
        "version": VERSION,
        "mode": "live_pilot_armed" if config["enabled"] else "disabled",
        "affects_execution": True,
        "automatic_promotion": False,
        "configuration": config,
        "validation": validation,
        "risk": base_risk,
        "funnel": dict(funnel),
        "matched_candidates": len(clones),
        "eligible_candidates": sum(
            bool((row.get("spot_flow_live_pilot") or {}).get("eligible"))
            for row in clones
        ),
        "recent_candidates": [
            {
                "ticker": row.get("ticker"),
                "asset": row.get("asset"),
                "side": row.get("side"),
                "entry_price": row.get("entry_price"),
                "tier": (row.get("spot_flow_live_pilot") or {}).get("tier"),
                "eligible": (row.get("spot_flow_live_pilot") or {}).get("eligible"),
                "reason": (row.get("spot_flow_live_pilot") or {}).get("reason"),
                "signal_matched_at": (row.get("spot_flow_live_pilot") or {}).get(
                    "signal_matched_at"
                ),
                "sizing": (row.get("spot_flow_live_pilot") or {}).get("sizing"),
            }
            for row in clones[:20]
        ],
    }
