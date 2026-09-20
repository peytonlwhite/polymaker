"""Locked four-day BTC 15-minute flow-veto research sprint.

This module is deliberately paper-only.  It records one-contract outcomes for
the candidate policy, non-promotable price/time diagnostics, and non-BTC
controls.  It has no order API imports and cannot alter bankroll or execution
state.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation


VERSION = "btc-15m-flow-veto-override-v1"
POLICY_NAME = "btc_15m_flow_veto_override"
MODE = "paper_shadow_only"
PRIMARY_LANE = "flow_veto_override"
EXACT_40_BASELINE = "exact_40_baseline"
BAND_40_44_BASELINE = "band_40_44_baseline"
LATE_2_4_DIAGNOSTIC = "btc_2_4_minute_diagnostic"
CONTROL_LANE = "asset_flow_veto_control"
ALLOWED_FLOW_REJECTIONS = {
    "directional_opposition",
    "directional_flow_too_weak",
}
CONTROL_ASSETS = {"ETH", "SOL", "XRP", "DOGE"}
ONE_SIDED_90_Z = 1.2815515655446004


def _hypothesis_registry(minimum_markets=50):
    target = max(50, _integer(minimum_markets, 50))
    return {
        EXACT_40_BASELINE: {
            "role": "primary_candidate",
            "asset": "BTC",
            "price_cents": 40.0,
            "minutes": [2.0, 12.0],
            "minimum_independent_markets": target,
        },
        LATE_2_4_DIAGNOSTIC: {
            "role": "primary_candidate",
            "asset": "BTC",
            "price_cents": [35.0, 49.0],
            "minutes": [2.0, 4.0],
            "minimum_independent_markets": target,
        },
        BAND_40_44_BASELINE: {
            "role": "negative_control",
            "asset": "BTC",
            "price_cents": [40.0, 44.0],
            "minutes": [2.0, 12.0],
            "minimum_independent_markets": target,
        },
    }


def _number(value, default=0.0):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _boolean(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_time(value):
    if value in (None, ""):
        return None
    try:
        return _now(value)
    except (TypeError, ValueError):
        return None


def _iso(value=None):
    return _now(value).isoformat()


def _decimal(value, default="0"):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _configuration_hash(configuration):
    payload = {
        key: value
        for key, value in configuration.items()
        if key != "policy_hash"
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def policy_configuration(settings=None):
    settings = settings or {}
    hypothesis_minimum = max(
        50,
        _integer(settings.get("CRYPTO_15M_SPRINT_HYPOTHESIS_MIN_MARKETS", 50), 50),
    )
    configuration = {
        "version": VERSION,
        "policy_name": POLICY_NAME,
        "enabled": _boolean(settings.get("CRYPTO_15M_SPRINT_ENABLED", True), True),
        "shadow_only": True,
        "automatic_promotion": False,
        "primary_asset": "BTC",
        "primary_series": "KXBTC15M",
        "control_assets": sorted(CONTROL_ASSETS),
        "allowed_rejection_reasons": sorted(ALLOWED_FLOW_REJECTIONS),
        "minimum_price_cents": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_PRICE_CENTS", 35), 35
        ),
        "maximum_price_cents": _number(
            settings.get("CRYPTO_15M_SPRINT_MAX_PRICE_CENTS", 49), 49
        ),
        "minimum_minutes_remaining": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_MINUTES_REMAINING", 2), 2
        ),
        "maximum_minutes_remaining": _number(
            settings.get("CRYPTO_15M_SPRINT_MAX_MINUTES_REMAINING", 12), 12
        ),
        "minimum_reserved_edge_cents": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_RESERVED_EDGE_CENTS", 2), 2
        ),
        "slippage_reserve_cents": _number(
            settings.get("CRYPTO_15M_SPRINT_SLIPPAGE_RESERVE_CENTS", 1), 1
        ),
        "minimum_data_quality": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_DATA_QUALITY", 0.95), 0.95
        ),
        "maximum_book_age_seconds": _number(
            settings.get("CRYPTO_15M_SPRINT_MAX_BOOK_AGE_SECONDS", 2), 2
        ),
        "minimum_top_depth_contracts": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_TOP_DEPTH_CONTRACTS", 1), 1
        ),
        "prospective_hours": _number(
            settings.get("CRYPTO_15M_SPRINT_PROSPECTIVE_HOURS", 72), 72
        ),
        "settlement_finalization_minutes": _number(
            settings.get("CRYPTO_15M_SPRINT_FINALIZATION_MINUTES", 30), 30
        ),
        "minimum_forward_markets": _integer(
            settings.get("CRYPTO_15M_SPRINT_MIN_FORWARD_MARKETS", 60), 60
        ),
        "minimum_reserved_roi_pct": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_RESERVED_ROI_PCT", 10), 10
        ),
        "minimum_break_even_advantage_pp": _number(
            settings.get("CRYPTO_15M_SPRINT_MIN_BREAK_EVEN_ADVANTAGE_PP", 5), 5
        ),
        "confidence_bound": 0.90,
        "contracts_per_record": 1,
        "baseline_lanes": [EXACT_40_BASELINE, BAND_40_44_BASELINE],
        "hypothesis_minimum_markets": hypothesis_minimum,
        "hypothesis_registry": _hypothesis_registry(hypothesis_minimum),
        "candidate_gate_snapshot": {
            "tiered_action_gate_enabled": _boolean(
                settings.get("CRYPTO_15M_TIERED_ACTION_GATE_ENABLED", True),
                True,
            ),
            "minimum_edge_cents": _number(
                settings.get("CRYPTO_15M_TIERED_MIN_EDGE", 3),
                3,
            ),
            "minimum_confidence": _number(
                settings.get("CRYPTO_15M_TIERED_MIN_CONFIDENCE", 70),
                70,
            ),
            "minimum_price_cents": _number(
                settings.get("CRYPTO_15M_TIERED_MIN_PRICE_CENTS", 35),
                35,
            ),
            "maximum_price_cents": _number(
                settings.get("CRYPTO_15M_TIERED_MAX_PRICE_CENTS", 70),
                70,
            ),
            "minimum_minutes_remaining": _number(
                settings.get("CRYPTO_15M_TIERED_MIN_MINUTES_REMAINING", 2),
                2,
            ),
            "maximum_minutes_remaining": _number(
                settings.get("CRYPTO_15M_TIERED_MAX_MINUTES_REMAINING", 12),
                12,
            ),
            "directional_confirmation_required": _boolean(
                settings.get("CRYPTO_15M_DIRECTIONAL_CONFIRMATION_REQUIRED", True),
                True,
            ),
            "directional_confirmation_minimum_sources": _integer(
                settings.get("CRYPTO_15M_DIRECTIONAL_CONFIRMATION_MIN_SOURCES", 2),
                2,
            ),
            "directional_confirmation_minimum_strength": _number(
                settings.get("CRYPTO_15M_DIRECTIONAL_CONFIRMATION_MIN_STRENGTH", 0.10),
                0.10,
            ),
        },
        "max_records": _integer(
            settings.get("CRYPTO_15M_SPRINT_MAX_RECORDS", 5000), 5000
        ),
    }
    configuration["policy_hash"] = _configuration_hash(configuration)
    return configuration


def empty_state(settings=None, now=None):
    current = _now(now)
    configuration = policy_configuration(settings)
    end = current + timedelta(hours=max(0.0, configuration["prospective_hours"]))
    evaluation_due = end + timedelta(
        minutes=max(0.0, configuration["settlement_finalization_minutes"])
    )
    return {
        "version": VERSION,
        "mode": MODE,
        "policy_name": POLICY_NAME,
        "configuration": configuration,
        "prospective_started_at": current.isoformat(),
        "prospective_ends_at": end.isoformat(),
        "evaluation_due_at": evaluation_due.isoformat(),
        "development_cutoff_at": current.isoformat(),
        "late_2_4_diagnostic_started_at": current.isoformat(),
        "hypotheses_registered_at": current.isoformat(),
        "updated_at": current.isoformat(),
        "records": [],
        "last_scan_funnel": {},
    }


def normalize_state(value, settings=None, now=None):
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return empty_state(settings, now=now)
    state = value
    state["version"] = VERSION
    state["mode"] = MODE
    state["policy_name"] = POLICY_NAME
    state["records"] = (
        state.get("records") if isinstance(state.get("records"), list) else []
    )
    configuration = state.get("configuration")
    if not isinstance(configuration, dict) or not configuration.get("policy_hash"):
        configuration = policy_configuration(settings)
        state["configuration"] = configuration
    elif not configuration.get("candidate_gate_snapshot") and not any(
        row.get("lane") == PRIMARY_LANE
        for row in state["records"]
        if isinstance(row, dict)
    ):
        # The initial sprint deployment predated the explicit snapshot of the
        # upstream campaign gates.  With no promotable forward records, it is
        # safe to lock those inputs without changing the already-started
        # collection clock.  Any already-captured baseline/control rows remain
        # non-promotable and are preserved for auditability.
        configuration = policy_configuration(settings)
        state["configuration"] = configuration
        state["configuration_locked_at"] = _iso(now)
        state["configuration_migration_note"] = (
            "candidate gates locked before first promotable observation; "
            "existing non-promotable records preserved"
        )
    if not configuration.get("hypothesis_registry"):
        minimum = max(
            50,
            _integer(
                (settings or {}).get("CRYPTO_15M_SPRINT_HYPOTHESIS_MIN_MARKETS", 50),
                50,
            ),
        )
        configuration["hypothesis_minimum_markets"] = minimum
        configuration["hypothesis_registry"] = _hypothesis_registry(minimum)
        configuration["policy_hash"] = _configuration_hash(configuration)
        state["configuration"] = configuration
        state["hypotheses_registered_at"] = _iso(now)
        state["hypothesis_registration_note"] = (
            "exact-40 and BTC 2-4 minute candidates frozen prospectively; "
            "40-44 retained as negative control; earlier observations excluded"
        )
    start = _parse_time(state.get("prospective_started_at")) or _now(now)
    end = _parse_time(state.get("prospective_ends_at")) or (
        start + timedelta(hours=max(0.0, _number(configuration.get("prospective_hours"), 72)))
    )
    evaluation_due = _parse_time(state.get("evaluation_due_at")) or (
        end
        + timedelta(
            minutes=max(
                0.0,
                _number(configuration.get("settlement_finalization_minutes"), 30),
            )
        )
    )
    state["prospective_started_at"] = start.isoformat()
    state["prospective_ends_at"] = end.isoformat()
    state["evaluation_due_at"] = evaluation_due.isoformat()
    state.setdefault("development_cutoff_at", start.isoformat())
    state.setdefault("late_2_4_diagnostic_started_at", _iso(now))
    state.setdefault("hypotheses_registered_at", start.isoformat())
    state.setdefault("last_scan_funnel", {})
    return state


def _selected_depth(candidate):
    side = str(candidate.get("side") or "").lower()
    book = candidate.get("kalshi_microstructure") or {}
    return _number(book.get(f"top1_{side}_depth_contracts"), 0.0)


def _selected_book_price(candidate):
    side = str(candidate.get("side") or "").lower()
    book = candidate.get("kalshi_microstructure") or {}
    return _number(book.get(f"best_{side}_entry_price_cents"), -1.0)


def capture_quality_review(candidate, configuration):
    reasons = []
    side = str(candidate.get("side") or "").lower()
    price = _number(candidate.get("entry_price"), -1)
    minutes = _number(candidate.get("minutes_to_close"), -1)
    book = candidate.get("kalshi_microstructure") or {}
    data_quality = candidate.get("data_quality") or {}
    close_time = _parse_time(candidate.get("close_time"))

    if not candidate.get("is_15m_market") or candidate.get("market_lane") != "crypto_15m":
        reasons.append("not_crypto_15m")
    if side not in {"yes", "no"}:
        reasons.append("invalid_side")
    if not 0 < price < 100:
        reasons.append("invalid_entry_price")
    if not (
        _number(configuration.get("minimum_minutes_remaining"), 2)
        <= minutes
        <= _number(configuration.get("maximum_minutes_remaining"), 12)
    ):
        reasons.append("outside_entry_window")
    if close_time is None:
        reasons.append("missing_close_time")
    if candidate.get("initial_price_source") != "kalshi_executable_orderbook":
        reasons.append("non_executable_price_source")
    if not _boolean(candidate.get("fee_schedule_exact"), False):
        reasons.append("fee_schedule_unverified")
    if candidate.get("exact_fee_cents") is None:
        reasons.append("exact_fee_missing")
    if not _boolean(book.get("sequence_valid"), False):
        reasons.append("book_sequence_invalid")
    if book.get("source") != "kalshi_websocket":
        reasons.append("book_not_websocket")
    if not _boolean(book.get("fresh"), False):
        reasons.append("book_stale")
    if _number(book.get("age_seconds"), math.inf) > _number(
        configuration.get("maximum_book_age_seconds"), 2
    ):
        reasons.append("book_too_old")
    depth = _selected_depth(candidate)
    if depth + 1e-9 < _number(configuration.get("minimum_top_depth_contracts"), 1):
        reasons.append("insufficient_one_contract_depth")
    book_price = _selected_book_price(candidate)
    if book_price < 0 or abs(book_price - price) > 0.01:
        reasons.append("entry_price_book_mismatch")
    if _number(data_quality.get("score"), 0) < _number(
        configuration.get("minimum_data_quality"), 0.95
    ):
        reasons.append("data_quality_below_minimum")
    if data_quality.get("reasons"):
        reasons.append("data_quality_rejected")

    return {
        "ok": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "sequence_valid": _boolean(book.get("sequence_valid"), False),
        "book_source": book.get("source"),
        "book_age_seconds": round(_number(book.get("age_seconds"), 0), 4),
        "book_timestamp": book.get("orderbook_fetched_at") or book.get("received_at"),
        "book_sequence": book.get("sequence") or book.get("exchange_ts_ms"),
        "selected_side_depth_contracts": round(depth, 6),
        "selected_side_book_price_cents": round(book_price, 4),
        "data_quality": round(_number(data_quality.get("score"), 0), 6),
    }


def flow_veto_policy_review(candidate, configuration, *, allow_controls=False):
    reasons = []
    asset = str(candidate.get("asset") or "").upper()
    if allow_controls:
        if asset not in CONTROL_ASSETS:
            reasons.append("not_control_asset")
    else:
        if asset != configuration.get("primary_asset"):
            reasons.append("not_primary_asset")
        if str(candidate.get("series_ticker") or "").upper() != str(
            configuration.get("primary_series") or ""
        ).upper():
            reasons.append("not_primary_series")

    price = _number(candidate.get("entry_price"), -1)
    if not (
        _number(configuration.get("minimum_price_cents"), 35)
        <= price
        <= _number(configuration.get("maximum_price_cents"), 49)
    ):
        reasons.append("outside_policy_price_range")

    skip_reasons = {
        str(reason)
        for reason in candidate.get("skip_reasons") or []
        if str(reason)
    }
    allowed = set(configuration.get("allowed_rejection_reasons") or [])
    if not skip_reasons.intersection(allowed):
        reasons.append("flow_veto_reason_absent")
    unexpected = sorted(skip_reasons - allowed)
    if unexpected:
        reasons.append("other_rejection_reasons_present")

    expected_edge = _number(
        candidate.get("expected_edge", candidate.get("edge")),
        -999,
    )
    reserved_edge = expected_edge - _number(
        configuration.get("slippage_reserve_cents"), 1
    )
    if reserved_edge + 1e-9 < _number(
        configuration.get("minimum_reserved_edge_cents"), 2
    ):
        reasons.append("reserved_edge_below_minimum")

    quality = capture_quality_review(candidate, configuration)
    reasons.extend(quality.get("reasons") or [])
    return {
        "ok": not reasons,
        "reasons": list(dict.fromkeys(reasons)),
        "unexpected_rejection_reasons": unexpected,
        "expected_edge_cents": round(expected_edge, 4),
        "reserved_edge_cents": round(reserved_edge, 4),
        "capture_quality": quality,
    }


def _new_record(candidate, lane, configuration, now, review):
    price = _number(candidate.get("entry_price"))
    fee_cents = max(0.0, _number(candidate.get("exact_fee_cents")))
    reserve_cents = max(0.0, _number(configuration.get("slippage_reserve_cents"), 1))
    book = candidate.get("kalshi_microstructure") or {}
    quality = review.get("capture_quality") or capture_quality_review(
        candidate, configuration
    )
    ticker = str(candidate.get("ticker") or "")
    hypothesis = (configuration.get("hypothesis_registry") or {}).get(lane) or {}
    return {
        "id": f"{lane}:{ticker}",
        "version": VERSION,
        "mode": MODE,
        "status": "open",
        "prospective": True,
        "strategy_owner": "crypto_btc_15m_sprint",
        "policy_name": POLICY_NAME,
        "policy_hash": configuration.get("policy_hash"),
        "lane": lane,
        "hypothesis_role": hypothesis.get("role"),
        "hypothesis_preregistered": bool(hypothesis),
        "eligible_for_promotion": lane == PRIMARY_LANE,
        "captured_at": _iso(now),
        "ticker": ticker,
        "event_ticker": candidate.get("event_ticker"),
        "series_ticker": candidate.get("series_ticker"),
        "asset": str(candidate.get("asset") or "").upper(),
        "side": str(candidate.get("side") or "").lower(),
        "entry_price_cents": round(price, 4),
        "entry_stake_dollars": round(price / 100.0, 6),
        "fee_per_contract_dollars": round(fee_cents / 100.0, 6),
        "slippage_reserve_dollars": round(reserve_cents / 100.0, 6),
        "unreserved_break_even_probability": round((price + fee_cents) / 100.0, 6),
        "reserved_break_even_probability": round(
            (price + fee_cents + reserve_cents) / 100.0,
            6,
        ),
        "model_prob_yes": candidate.get("model_prob_yes"),
        "selected_side_probability": candidate.get("selected_side_probability"),
        "selected_side_probability_low": candidate.get("selected_side_probability_low"),
        "selected_side_probability_high": candidate.get("selected_side_probability_high"),
        "expected_edge_cents": review.get(
            "expected_edge_cents",
            candidate.get("expected_edge", candidate.get("edge")),
        ),
        "reserved_edge_cents": review.get("reserved_edge_cents"),
        "skip_reasons": list(candidate.get("skip_reasons") or []),
        "directional_evidence": {
            "flow_review": candidate.get("flow_review") or {},
            "directional_confirmation": candidate.get("directional_confirmation") or {},
        },
        "capture_quality": quality,
        "book": {
            "source": book.get("source"),
            "sequence_valid": book.get("sequence_valid"),
            "sequence": book.get("sequence") or book.get("exchange_ts_ms"),
            "timestamp": book.get("orderbook_fetched_at") or book.get("received_at"),
            "age_seconds": book.get("age_seconds"),
            "message_type": book.get("message_type"),
            "selected_side_depth_contracts": _selected_depth(candidate),
            "selected_side_entry_price_cents": _selected_book_price(candidate),
        },
        "market_quote_fetched_at": candidate.get("market_quote_fetched_at"),
        "close_time": candidate.get("close_time"),
        "minutes_to_close": round(_number(candidate.get("minutes_to_close")), 4),
        "result": None,
        "market_result": None,
        "settlement_verified": False,
        "unreserved_profit": None,
        "reserved_profit": None,
    }


def capture_candidates(state, candidates, *, now=None):
    current = _now(now)
    configuration = state.get("configuration") or {}
    end = _parse_time(state.get("prospective_ends_at")) or current
    funnel = {
        "reviewed": 0,
        "captured": 0,
        "captured_by_lane": {},
        "rejection_reasons": {},
        "capture_window_open": current < end,
    }
    if current >= end:
        state["last_scan_funnel"] = funnel
        return []

    known = {str(row.get("id") or "") for row in state.get("records") or []}
    added = []
    for candidate in candidates or []:
        if candidate.get("market_lane") != "crypto_15m":
            continue
        funnel["reviewed"] += 1
        asset = str(candidate.get("asset") or "").upper()
        price = _number(candidate.get("entry_price"), -1)
        minutes = _number(candidate.get("minutes_to_close"), -1)
        quality = capture_quality_review(candidate, configuration)
        primary_review = flow_veto_policy_review(candidate, configuration)
        control_review = flow_veto_policy_review(
            candidate,
            configuration,
            allow_controls=True,
        )
        lanes = []
        if primary_review.get("ok"):
            lanes.append((PRIMARY_LANE, primary_review))
        elif asset == configuration.get("primary_asset"):
            for reason in primary_review.get("reasons") or []:
                funnel["rejection_reasons"][reason] = (
                    funnel["rejection_reasons"].get(reason, 0) + 1
                )
        if control_review.get("ok"):
            lanes.append((CONTROL_LANE, control_review))
        if asset == configuration.get("primary_asset") and quality.get("ok"):
            if abs(price - 40.0) <= 0.0001:
                lanes.append((EXACT_40_BASELINE, {"capture_quality": quality}))
            if 40.0 <= price <= 44.0:
                lanes.append((BAND_40_44_BASELINE, {"capture_quality": quality}))
            if (
                _number(configuration.get("minimum_price_cents"), 35)
                <= price
                <= _number(configuration.get("maximum_price_cents"), 49)
                and 2.0 <= minutes <= 4.0
            ):
                lanes.append((LATE_2_4_DIAGNOSTIC, {"capture_quality": quality}))

        for lane, review in lanes:
            record_id = f"{lane}:{candidate.get('ticker') or ''}"
            if not candidate.get("ticker") or record_id in known:
                continue
            row = _new_record(candidate, lane, configuration, current, review)
            state["records"].append(row)
            known.add(record_id)
            added.append(row)
            funnel["captured"] += 1
            funnel["captured_by_lane"][lane] = (
                funnel["captured_by_lane"].get(lane, 0) + 1
            )

    max_records = max(0, _integer(configuration.get("max_records"), 5000))
    if max_records and len(state["records"]) > max_records:
        open_rows = [row for row in state["records"] if row.get("status") == "open"]
        settled_rows = [row for row in state["records"] if row.get("status") != "open"]
        settled_rows = settled_rows[-max(0, max_records - len(open_rows)):]
        state["records"] = [*settled_rows, *open_rows]
    state["last_scan_funnel"] = funnel
    return added


def settle_records(
    state,
    fetch_market,
    *,
    now=None,
    grace_minutes=2.0,
    retry_minutes=5.0,
    max_checks=50,
):
    current = _now(now)
    settled = []
    checks = 0
    for row in state.get("records") or []:
        if row.get("status") != "open" or checks >= max(0, int(max_checks)):
            continue
        close_time = _parse_time(row.get("close_time"))
        if close_time is None or current < close_time + timedelta(minutes=max(0.0, grace_minutes)):
            continue
        last_check = _parse_time(row.get("last_settlement_check_at"))
        if last_check and current < last_check + timedelta(minutes=max(0.0, retry_minutes)):
            continue
        checks += 1
        row["last_settlement_check_at"] = current.isoformat()
        row["settlement_check_count"] = int(row.get("settlement_check_count") or 0) + 1
        try:
            market = fetch_market(row.get("ticker")) or {}
        except Exception as exc:
            row["last_settlement_error"] = f"{type(exc).__name__}: {exc}"[:300]
            continue
        status = str(market.get("status") or "").lower()
        result = str(market.get("result") or market.get("market_result") or "").lower()
        if status != "finalized" or result not in {"yes", "no"}:
            row["last_settlement_status"] = status or "not_finalized"
            continue

        stake = _decimal(row.get("entry_stake_dollars"))
        fee = _decimal(row.get("fee_per_contract_dollars"))
        reserve = _decimal(row.get("slippage_reserve_dollars"))
        payout = Decimal("1") if str(row.get("side") or "").lower() == result else Decimal("0")
        unreserved_profit = payout - stake - fee
        reserved_profit = unreserved_profit - reserve
        row.update({
            "status": "settled",
            "settled_at": market.get("settlement_ts") or market.get("settled_time") or current.isoformat(),
            "settlement_source": "kalshi_finalized_market",
            "settlement_verified": True,
            "market_result": result,
            "result": "WIN" if payout else "LOSS",
            "payout_dollars": round(float(payout), 6),
            "unreserved_profit": round(float(unreserved_profit), 6),
            "reserved_profit": round(float(reserved_profit), 6),
            "last_settlement_error": None,
            "last_settlement_status": "finalized",
        })
        settled.append(row)
    return settled


def _lower_confidence_bound(values):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    return mean - ONE_SIDED_90_Z * standard_error


def _max_drawdown(values):
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def _lane_summary(records, lane):
    rows = [row for row in records if row.get("lane") == lane]
    rows.sort(key=lambda row: str(row.get("captured_at") or ""))
    settled = [
        row
        for row in rows
        if row.get("status") == "settled" and row.get("settlement_verified")
    ]
    wins = sum(row.get("result") == "WIN" for row in settled)
    entry_stake = sum(_number(row.get("entry_stake_dollars")) for row in settled)
    fees = sum(_number(row.get("fee_per_contract_dollars")) for row in settled)
    reserves = sum(_number(row.get("slippage_reserve_dollars")) for row in settled)
    unreserved_profit = sum(_number(row.get("unreserved_profit")) for row in settled)
    reserved_values = [_number(row.get("reserved_profit")) for row in settled]
    reserved_profit = sum(reserved_values)
    actual_cost = entry_stake + fees
    reserved_cost = actual_cost + reserves
    win_rate = (100.0 * wins / len(settled)) if settled else 0.0
    break_even = (
        100.0
        * sum(_number(row.get("reserved_break_even_probability")) for row in settled)
        / len(settled)
        if settled
        else 0.0
    )
    lower_bound = _lower_confidence_bound(reserved_values)
    return {
        "lane": lane,
        "eligible_for_promotion": lane == PRIMARY_LANE,
        "tracked": len(rows),
        "open": sum(row.get("status") == "open" for row in rows),
        "settled": len(settled),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(win_rate, 4),
        "entry_stake": round(entry_stake, 6),
        "fees": round(fees, 6),
        "slippage_reserve": round(reserves, 6),
        "unreserved_profit": round(unreserved_profit, 6),
        "reserved_profit": round(reserved_profit, 6),
        "unreserved_roi": round(100.0 * unreserved_profit / actual_cost, 4) if actual_cost else 0.0,
        "reserved_roi": round(100.0 * reserved_profit / reserved_cost, 4) if reserved_cost else 0.0,
        "reserved_break_even_win_rate": round(break_even, 4),
        "break_even_advantage_pp": round(win_rate - break_even, 4),
        "mean_reserved_profit_per_contract": round(statistics.fmean(reserved_values), 6) if reserved_values else 0.0,
        "one_sided_90_lower_bound_profit": round(lower_bound, 6) if lower_bound is not None else None,
        "maximum_drawdown": round(_max_drawdown(reserved_values), 6),
        "verified_settlements": len(settled),
        "capture_quality_failures": sum(
            not _boolean((row.get("capture_quality") or {}).get("ok"), False)
            for row in rows
        ),
    }


def _period_summary(records, start, end):
    rows = []
    for row in records:
        if row.get("lane") != PRIMARY_LANE:
            continue
        captured = _parse_time(row.get("captured_at"))
        if captured is None or captured < start or captured >= end:
            continue
        if row.get("status") == "settled" and row.get("settlement_verified"):
            rows.append(row)
    profit = sum(_number(row.get("reserved_profit")) for row in rows)
    return {
        "settled": len(rows),
        "reserved_profit": round(profit, 6),
        "profitable": bool(rows and profit > 0),
    }


def development_summary(rejection_records, configuration, *, cutoff=None):
    cutoff_time = _parse_time(cutoff)
    eligible = []
    seen = set()
    for row in sorted(
        rejection_records or [],
        key=lambda value: str(value.get("captured_at") or ""),
    ):
        ticker = str(row.get("ticker") or "")
        captured = _parse_time(row.get("captured_at"))
        if not ticker or ticker in seen:
            continue
        if cutoff_time and (captured is None or captured >= cutoff_time):
            continue
        if row.get("status") != "settled" or str(row.get("asset") or "").upper() != "BTC":
            continue
        if str(row.get("series_ticker") or "").upper() != str(
            configuration.get("primary_series") or ""
        ).upper():
            continue
        skip_reasons = {str(reason) for reason in row.get("skip_reasons") or []}
        allowed = set(configuration.get("allowed_rejection_reasons") or [])
        if not skip_reasons.intersection(allowed) or skip_reasons - allowed:
            continue
        price = _number(row.get("entry_price"), -1)
        minutes = _number(row.get("minutes_to_close"), -1)
        edge = _number(row.get("edge"), -999)
        reserve_cents = _number(configuration.get("slippage_reserve_cents"), 1)
        if not (
            _number(configuration.get("minimum_price_cents"), 35)
            <= price
            <= _number(configuration.get("maximum_price_cents"), 49)
            and _number(configuration.get("minimum_minutes_remaining"), 2)
            <= minutes
            <= _number(configuration.get("maximum_minutes_remaining"), 12)
            and edge - reserve_cents
            >= _number(configuration.get("minimum_reserved_edge_cents"), 2)
            and row.get("fee_per_contract") is not None
        ):
            continue
        seen.add(ticker)
        fee = max(0.0, _number(row.get("fee_per_contract")))
        reserve = reserve_cents / 100.0
        payout = 1.0 if row.get("result") == "WIN" else 0.0
        unreserved = payout - (price / 100.0) - fee
        eligible.append({
            "lane": PRIMARY_LANE,
            "status": "settled",
            "settlement_verified": True,
            "capture_quality": {"ok": True, "source": "legacy_development_replay"},
            "captured_at": row.get("captured_at"),
            "result": row.get("result"),
            "entry_stake_dollars": price / 100.0,
            "fee_per_contract_dollars": fee,
            "slippage_reserve_dollars": reserve,
            "reserved_break_even_probability": (price / 100.0) + fee + reserve,
            "unreserved_profit": unreserved,
            "reserved_profit": unreserved - reserve,
        })
    summary = _lane_summary(eligible, PRIMARY_LANE)
    development_signal_positive = bool(summary.get("eligible_for_promotion"))
    summary.update({
        "provenance": "crypto_rejection_shadow_chronological_first_ticker_replay",
        "development_only": True,
        "counts_toward_qualification": False,
        "eligible_for_promotion": False,
        "development_signal_positive": development_signal_positive,
        "cutoff_at": cutoff,
    })
    return summary


def summarize(state, *, now=None, rejection_records=None):
    current = _now(now)
    configuration = state.get("configuration") or {}
    records = state.get("records") or []
    start = _parse_time(state.get("prospective_started_at")) or current
    end = _parse_time(state.get("prospective_ends_at")) or current
    evaluation_due = _parse_time(state.get("evaluation_due_at")) or end
    midpoint = start + ((end - start) / 2)
    lanes = [
        _lane_summary(records, PRIMARY_LANE),
        _lane_summary(records, EXACT_40_BASELINE),
        _lane_summary(records, BAND_40_44_BASELINE),
        _lane_summary(records, LATE_2_4_DIAGNOSTIC),
        _lane_summary(records, CONTROL_LANE),
    ]
    registered_at = _parse_time(state.get("hypotheses_registered_at")) or start
    hypotheses = []
    for lane, definition in (configuration.get("hypothesis_registry") or {}).items():
        forward_rows = [
            row for row in records
            if row.get("lane") == lane
            and (_parse_time(row.get("captured_at")) or current) >= registered_at
        ]
        lane_result = _lane_summary(forward_rows, lane)
        required = max(
            50,
            _integer(definition.get("minimum_independent_markets"), 50),
        )
        hypotheses.append({
            **lane_result,
            "role": definition.get("role"),
            "definition": definition,
            "registered_at": registered_at.isoformat(),
            "pre_registration_records_excluded": sum(
                row.get("lane") == lane for row in records
            ) - len(forward_rows),
            "minimum_independent_markets": required,
            "ready_for_manual_review": lane_result.get("settled", 0) >= required,
            "automatic_promotion": False,
        })
    primary = lanes[0]
    first_half = _period_summary(records, start, midpoint)
    second_half = _period_summary(records, midpoint, end + timedelta(microseconds=1))
    stored_hash = configuration.get("policy_hash")
    configuration_locked = bool(
        stored_hash and stored_hash == _configuration_hash(configuration)
    )
    candidate_rows = [row for row in records if row.get("lane") == PRIMARY_LANE]
    verification_ok = bool(
        primary.get("capture_quality_failures") == 0
        and primary.get("verified_settlements") == primary.get("settled")
        and all(row.get("status") == "settled" for row in candidate_rows)
    )
    gates = {
        "minimum_forward_markets": {
            "passed": primary.get("settled", 0)
            >= _integer(configuration.get("minimum_forward_markets"), 60),
            "actual": primary.get("settled", 0),
            "required": _integer(configuration.get("minimum_forward_markets"), 60),
        },
        "positive_reserved_profit": {
            "passed": primary.get("reserved_profit", 0) > 0,
            "actual": primary.get("reserved_profit", 0),
            "required": "> 0",
        },
        "minimum_reserved_roi": {
            "passed": primary.get("reserved_roi", 0)
            >= _number(configuration.get("minimum_reserved_roi_pct"), 10),
            "actual": primary.get("reserved_roi", 0),
            "required": _number(configuration.get("minimum_reserved_roi_pct"), 10),
        },
        "break_even_advantage": {
            "passed": primary.get("break_even_advantage_pp", 0)
            >= _number(configuration.get("minimum_break_even_advantage_pp"), 5),
            "actual": primary.get("break_even_advantage_pp", 0),
            "required": _number(configuration.get("minimum_break_even_advantage_pp"), 5),
        },
        "positive_one_sided_90_lower_bound": {
            "passed": (
                primary.get("one_sided_90_lower_bound_profit") is not None
                and primary.get("one_sided_90_lower_bound_profit") > 0
            ),
            "actual": primary.get("one_sided_90_lower_bound_profit"),
            "required": "> 0",
        },
        "both_halves_profitable": {
            "passed": first_half.get("profitable") and second_half.get("profitable"),
            "first_half": first_half,
            "second_half": second_half,
        },
        "capture_and_settlement_verification": {
            "passed": verification_ok,
            "capture_quality_failures": primary.get("capture_quality_failures", 0),
            "verified_settlements": primary.get("verified_settlements", 0),
            "tracked": primary.get("tracked", 0),
        },
        "configuration_locked": {
            "passed": configuration_locked,
            "policy_hash": stored_hash,
        },
    }
    if current < end:
        status = "COLLECTING"
    elif current < evaluation_due and primary.get("open", 0) > 0:
        status = "FINALIZING"
    elif primary.get("settled", 0) < _integer(
        configuration.get("minimum_forward_markets"), 60
    ):
        status = "INCONCLUSIVE"
    elif all(review.get("passed") for review in gates.values()):
        status = "PASS"
    else:
        status = "FAIL"

    controls = []
    for asset in sorted(CONTROL_ASSETS):
        asset_records = [
            row
            for row in records
            if row.get("lane") == CONTROL_LANE and row.get("asset") == asset
        ]
        summary = _lane_summary(asset_records, CONTROL_LANE)
        summary["asset"] = asset
        summary["eligible_for_promotion"] = False
        controls.append(summary)
    remaining_seconds = max(0.0, (end - current).total_seconds())
    duration_seconds = max(1.0, (end - start).total_seconds())
    return {
        "version": VERSION,
        "policy_name": POLICY_NAME,
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "status": status,
        "recommendation_only": status == "PASS",
        "prospective_started_at": start.isoformat(),
        "prospective_ends_at": end.isoformat(),
        "evaluation_due_at": evaluation_due.isoformat(),
        "remaining_seconds": round(remaining_seconds, 3),
        "progress_pct": round(100.0 * min(1.0, max(0.0, (current - start).total_seconds() / duration_seconds)), 2),
        "configuration": configuration,
        "hypotheses_registered_at": registered_at.isoformat(),
        "hypotheses": hypotheses,
        "primary": primary,
        "lanes": lanes,
        "diagnostics": {
            "btc_2_4_minute": {
                **next(
                    row for row in lanes
                    if row.get("lane") == LATE_2_4_DIAGNOSTIC
                ),
                "started_at": state.get("late_2_4_diagnostic_started_at"),
                "development_selected": False,
                "preregistered_hypothesis": True,
                "eligible_for_promotion": False,
                "promotion_requires_manual_review": True,
            },
        },
        "controls_by_asset": controls,
        "first_half": first_half,
        "second_half": second_half,
        "qualification_gates": gates,
        "development": development_summary(
            rejection_records or [],
            configuration,
            cutoff=state.get("development_cutoff_at"),
        ),
        "candidate_funnel": state.get("last_scan_funnel") or {},
        "recent_records": sorted(
            records,
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
        "updated_at": state.get("updated_at"),
    }


def run_scan(
    state,
    candidates,
    fetch_market,
    *,
    settings=None,
    rejection_records=None,
    now=None,
):
    current = _now(now)
    state = normalize_state(state, settings=settings, now=current)
    settled = settle_records(
        state,
        fetch_market,
        now=current,
        grace_minutes=_number((settings or {}).get("CRYPTO_15M_SPRINT_SETTLEMENT_GRACE_MINUTES", 2), 2),
        retry_minutes=_number((settings or {}).get("CRYPTO_15M_SPRINT_SETTLEMENT_RETRY_MINUTES", 5), 5),
        max_checks=_integer((settings or {}).get("CRYPTO_15M_SPRINT_MAX_SETTLEMENT_CHECKS_PER_SCAN", 50), 50),
    )
    added = capture_candidates(state, candidates, now=current)
    state["updated_at"] = current.isoformat()
    report = summarize(
        state,
        now=current,
        rejection_records=rejection_records,
    )
    report["captured_this_scan"] = len(added)
    report["settled_this_scan"] = len(settled)
    return state, report, added, settled
