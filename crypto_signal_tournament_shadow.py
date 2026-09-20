"""Preregistered, paper-only tournament for orthogonal crypto 15-minute signals.

Each signal freezes a one-contract primary arm and the simultaneous opposite
arm from the same executable book.  The consensus lane also records an exact-
fee, integer-contract sizing counterfactual based only on the number of agreeing
signals.  This module has no order, bankroll, recovery, or live-promotion API.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import NormalDist
from zoneinfo import ZoneInfo

from crypto_pricing import kalshi_order_fee
from crypto_evidence import retain_records, register_policy


VERSION = "crypto-signal-tournament-shadow-v1"
MODE = "paper_shadow_only"
EXECUTION_PARITY_VERSION = "crypto-signal-execution-parity-v1"
CHICAGO = ZoneInfo("America/Chicago")
BASE_LANES = (
    "cross_venue_flow_follow",
    "cross_venue_impulse_follow",
    "spot_leads_kalshi",
    "cross_venue_book_pressure",
    "kalshi_trade_alignment",
    "kalshi_overreaction_fade",
)
CONSENSUS_LANE = "multi_signal_consensus"
CONTEMPORANEOUS_SPOT_FLOW_LANE = "spot_flow_contemporaneous"
LANES = (*BASE_LANES, CONSENSUS_LANE)
FAMILYWISE_CONFIDENCE = 0.95
PER_HYPOTHESIS_CONFIDENCE = 1.0 - (1.0 - FAMILYWISE_CONFIDENCE) / len(LANES)
FAMILY_Z = NormalDist().inv_cdf(PER_HYPOTHESIS_CONFIDENCE)


def _number(value, default=0.0):
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
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


def _iso(value=None):
    return _now(value).isoformat()


def _observation_date(value):
    try:
        return _now(value).astimezone(CHICAGO).date().isoformat()
    except (TypeError, ValueError):
        return None


def _side(direction):
    return "yes" if _number(direction) > 0 else "no"


def _sign(value, threshold=0.0):
    number = _number(value)
    if number > threshold:
        return 1
    if number < -threshold:
        return -1
    return 0


def _policy_hash(config):
    payload = {key: value for key, value in config.items() if key != "policy_hash"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def configuration(settings=None):
    settings = settings or {}
    assets = sorted(
        {
            value.strip().upper()
            for value in str(
                settings.get(
                    "CRYPTO_SIGNAL_TOURNAMENT_ASSETS",
                    "BTC,ETH,SOL,DOGE,XRP",
                )
            ).split(",")
            if value.strip()
        }
    )
    config = {
        "enabled": _boolean(
            settings.get("CRYPTO_SIGNAL_TOURNAMENT_ENABLED", True), True
        ),
        "assets": assets,
        "minimum_minutes": max(
            0.0,
            _number(settings.get("CRYPTO_SIGNAL_TOURNAMENT_MIN_MINUTES", 2), 2),
        ),
        "maximum_minutes": max(
            0.0,
            _number(settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_MINUTES", 13), 13),
        ),
        "minimum_price_cents": max(
            1.0,
            _number(settings.get("CRYPTO_SIGNAL_TOURNAMENT_MIN_PRICE_CENTS", 20), 20),
        ),
        "maximum_price_cents": min(
            99.0,
            _number(settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_PRICE_CENTS", 80), 80),
        ),
        "minimum_data_quality": max(
            0.0,
            min(
                1.0,
                _number(
                    settings.get("CRYPTO_SIGNAL_TOURNAMENT_MIN_DATA_QUALITY", 0.90),
                    0.90,
                ),
            ),
        ),
        "maximum_quote_age_seconds": max(
            0.1,
            _number(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_QUOTE_AGE_SECONDS", 2),
                2,
            ),
        ),
        "maximum_source_age_seconds": max(
            0.1,
            _number(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_SOURCE_AGE_SECONDS", 4),
                4,
            ),
        ),
        "maximum_spread_cents": max(
            0.0,
            _number(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_SPREAD_CENTS", 4),
                4,
            ),
        ),
        "minimum_unique_markets_interim": max(
            25,
            _integer(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_INTERIM_MARKETS", 100),
                100,
            ),
        ),
        "minimum_observation_days_interim": max(
            3,
            _integer(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_INTERIM_DAYS", 7), 7
            ),
        ),
        "minimum_unique_markets_final": max(
            100,
            _integer(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_FINAL_MARKETS", 200),
                200,
            ),
        ),
        "minimum_observation_days_final": max(
            14,
            _integer(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_FINAL_DAYS", 30), 30
            ),
        ),
        "settlement_grace_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_SETTLEMENT_GRACE_MINUTES", 2),
                2,
            ),
        ),
        "settlement_retry_minutes": max(
            0.0,
            _number(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_SETTLEMENT_RETRY_MINUTES", 5),
                5,
            ),
        ),
        "maximum_settlement_checks_per_scan": max(
            1,
            _integer(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_SETTLEMENT_CHECKS", 50),
                50,
            ),
        ),
        "maximum_records": max(
            1000,
            _integer(
                settings.get("CRYPTO_SIGNAL_TOURNAMENT_MAX_RECORDS", 15000),
                15000,
            ),
        ),
        "familywise_confidence_level": FAMILYWISE_CONFIDENCE,
        "per_hypothesis_one_sided_confidence_level": round(
            PER_HYPOTHESIS_CONFIDENCE, 8
        ),
        "historical_backfill": False,
        "execution_revision": "crypto-audit-2026-09-v1",
        "affects_execution": False,
        "automatic_promotion": False,
        "lanes": list(LANES),
    }
    config["policy_hash"] = _policy_hash(config)
    return config


def empty_ledger(settings=None, now=None):
    config = configuration(settings)
    registered = _iso(now)
    return {
        "version": VERSION,
        "mode": MODE,
        "registered_at": registered,
        "updated_at": registered,
        "configuration": config,
        "records": [],
        "last_capture_funnel": {},
        "last_capture_ids": [],
        "last_settled_ids": [],
    }


def normalize_ledger(value, settings=None, now=None):
    ledger = value if isinstance(value, dict) else {}
    config = configuration(settings)
    ledger["version"] = VERSION
    ledger["mode"] = MODE
    ledger.setdefault("registered_at", _iso(now))
    register_policy(ledger, config, _iso(now))
    ledger["records"] = (
        ledger.get("records") if isinstance(ledger.get("records"), list) else []
    )
    ledger.setdefault("last_capture_funnel", {})
    ledger.setdefault("last_capture_ids", [])
    ledger.setdefault("last_settled_ids", [])
    # Execution parity is intentionally prospective.  Existing signal records
    # keep their original snapshot-only results and are never backfilled with
    # prices that were not observed at the time an order would have been sent.
    ledger.setdefault("execution_parity_registered_at", _iso(now))
    ledger["live_pipeline_attempts"] = (
        ledger.get("live_pipeline_attempts")
        if isinstance(ledger.get("live_pipeline_attempts"), list)
        else []
    )
    for row in ledger["records"]:
        if not isinstance(row, dict):
            continue
        row["affects_execution"] = False
        row["automatic_promotion"] = False
        row.setdefault("historical_backfill", False)
        if isinstance(row.get("execution_parity"), dict):
            row["execution_parity"]["affects_execution"] = False
            row["execution_parity"]["automatic_promotion"] = False
    return ledger


def attach_execution_parity(
    ledger,
    records,
    refresh_quote,
    settings=None,
    now=None,
):
    """Add a fresh-book taker/FOK counterfactual to new shadow records.

    ``refresh_quote`` is supplied by the live scanner so the shadow uses the
    same Kalshi book source and depth parser as production.  This function has
    no order API and cannot affect execution.  The one-contract assumption is
    deliberate: it tests whether the signal survives real executable pricing
    before any bankroll or staking hypothesis is layered on top.
    """
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    current = _now(now)
    maximum_adverse_move = max(
        0.0,
        _number(
            (settings or {}).get(
                "CRYPTO_SIGNAL_TOURNAMENT_EXECUTION_PARITY_MAX_ADVERSE_MOVE_CENTS",
                1,
            ),
            1,
        ),
    )
    updates = []
    for row in records or []:
        if not isinstance(row, dict) or row.get("execution_parity"):
            continue
        primary = row.get("primary") or {}
        side = str(primary.get("side") or "").lower()
        initial_price = _number(primary.get("entry_price_cents"), -1.0)
        reasons = []
        try:
            refreshed = refresh_quote(str(row.get("ticker") or ""), side, 1) or {}
        except Exception as exc:
            refreshed = {
                "ok": False,
                "error": "execution_parity_quote_unavailable",
                "message": type(exc).__name__,
            }
        refreshed_price_raw = refreshed.get("full_size_entry_price_cents")
        if refreshed_price_raw is None:
            refreshed_price_raw = refreshed.get("best_entry_price_cents")
        refreshed_price = (
            _number(refreshed_price_raw, -1.0)
            if refreshed_price_raw is not None
            else -1.0
        )
        available = _number(refreshed.get("available_contracts"))
        if refreshed.get("error"):
            reasons.append(str(refreshed.get("error")))
        if refreshed_price <= 0:
            reasons.append("execution_parity_no_executable_price")
        if available + 1e-9 < 1:
            reasons.append("execution_parity_insufficient_fok_depth")
        if refreshed_price > 0 and not (
            config["minimum_price_cents"]
            <= refreshed_price
            <= config["maximum_price_cents"]
        ):
            reasons.append("execution_parity_price_outside_band")
        adverse_move = (
            refreshed_price - initial_price
            if refreshed_price > 0 and initial_price > 0
            else None
        )
        if (
            adverse_move is not None
            and adverse_move > maximum_adverse_move + 1e-9
        ):
            reasons.append("execution_parity_adverse_move")
        schedule = primary.get("fee_schedule") or {}
        fee = (
            kalshi_order_fee(
                refreshed_price,
                1,
                schedule=schedule,
                liquidity_role="taker",
            )
            if refreshed_price > 0
            else {}
        )
        if not fee.get("exact") or not fee.get("authoritative"):
            reasons.append("execution_parity_fee_not_exact")
        reasons = list(dict.fromkeys(reasons))
        arm = None
        if not reasons:
            fee_dollars = _number(fee.get("fee_dollars"))
            arm = {
                "side": side,
                "contracts": 1,
                "entry_price_cents": round(refreshed_price, 4),
                "fee_dollars": round(fee_dollars, 6),
                "total_cost_dollars": round(
                    refreshed_price / 100.0 + fee_dollars,
                    6,
                ),
                "result": None,
                "won": None,
                "virtual_profit": None,
            }
        captured_at = _now(row.get("captured_at"))
        parity = {
            "version": EXECUTION_PARITY_VERSION,
            "mode": "production_book_shadow",
            "source": "shared_kalshi_book_and_depth_pipeline",
            "fill_model": "visible_taker_fok_depth",
            "affects_execution": False,
            "automatic_promotion": False,
            "historical_backfill": False,
            "evaluated_at": current.isoformat(),
            "capture_to_refresh_seconds": round(
                max(0.0, (current - captured_at).total_seconds()), 6
            ),
            "initial_entry_price_cents": initial_price,
            "refreshed_entry_price_cents": (
                round(refreshed_price, 4) if refreshed_price > 0 else None
            ),
            "adverse_move_cents": (
                round(adverse_move, 4) if adverse_move is not None else None
            ),
            "maximum_adverse_move_cents": maximum_adverse_move,
            "required_contracts": 1,
            "available_contracts": available,
            "fetched_at": refreshed.get("fetched_at"),
            "status": "book_fillable" if not reasons else "rejected",
            "would_submit": not reasons,
            "reason": reasons[0] if reasons else "book_fillable",
            "reasons": reasons,
            "arm": arm,
        }
        row["execution_parity"] = parity
        updates.append(row)
    if updates:
        ledger["updated_at"] = current.isoformat()
    return updates


def apply_live_pipeline_parity(
    ledger,
    candidate,
    quote_refresh,
    live_order=None,
    settings=None,
    now=None,
):
    """Replace book-only parity with the exact promoted live-pipeline result.

    Tier A maps to the contemporaneous spot+flow record and Tier B maps to the
    standalone spot-lead record.  This avoids double-counting one live decision
    while preserving the other research lanes as separate hypotheses.
    """
    ledger = normalize_ledger(ledger, settings, now)
    review = (candidate or {}).get("spot_flow_live_pilot") or {}
    tier = str(review.get("tier") or "")
    lane = (
        CONTEMPORANEOUS_SPOT_FLOW_LANE
        if tier == "spot_flow_confirmed"
        else "spot_leads_kalshi"
    )
    ticker = str((candidate or {}).get("ticker") or "")
    matched_at = _now(
        review.get("signal_matched_at")
        or (candidate or {}).get("captured_at")
        or now
    )
    eligible_rows = [
        row
        for row in ledger.get("records") or []
        if row.get("lane") == lane and str(row.get("ticker") or "") == ticker
    ]
    eligible_rows.sort(key=lambda row: str(row.get("captured_at") or ""), reverse=True)
    target = next(
        (
            row
            for row in eligible_rows
            if abs((_now(row.get("captured_at")) - matched_at).total_seconds()) <= 90
        ),
        None,
    )
    refresh = quote_refresh or {}
    refreshed_price = refresh.get("refreshed_price_cents")
    if refreshed_price is None:
        refreshed_price = refresh.get("full_size_entry_price_cents")
    reasons = list(review.get("reasons") or [])
    if not refresh.get("ok"):
        reasons.append(str(refresh.get("error") or "live_pipeline_quote_rejected"))
    order = live_order or {}
    if live_order is not None and not order.get("ok"):
        reasons.append(str(order.get("error") or "live_pipeline_order_failed"))
    reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    size = review.get("sizing") or {}
    contracts = max(0, _integer(size.get("contracts")))
    fee_dollars = _number(size.get("expected_fee_dollars"))
    arm = None
    would_submit = bool(refresh.get("ok") and contracts > 0 and not reasons)
    if would_submit and refreshed_price is not None:
        arm = {
            "side": str((candidate or {}).get("side") or "").lower(),
            "contracts": contracts,
            "entry_price_cents": round(_number(refreshed_price), 4),
            "fee_dollars": round(fee_dollars, 6),
            "total_cost_dollars": round(
                contracts * _number(refreshed_price) / 100.0 + fee_dollars,
                6,
            ),
            "result": None,
            "won": None,
            "virtual_profit": None,
        }
    actual_status = None
    if live_order is not None:
        actual_status = "filled" if order.get("ok") else "order_failed"
    parity = {
        "version": EXECUTION_PARITY_VERSION,
        "mode": "production_pipeline_shadow",
        "source": "exact_live_spot_flow_pipeline",
        "fill_model": "live_signal_refresh_risk_size_depth_and_fok",
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "evaluated_at": _iso(now),
        "initial_entry_price_cents": refresh.get("original_price_cents"),
        "refreshed_entry_price_cents": refreshed_price,
        "adverse_move_cents": refresh.get("adverse_move_cents"),
        "required_contracts": contracts,
        "available_contracts": refresh.get("available_contracts"),
        "signal_to_refresh_seconds": refresh.get("signal_to_refresh_seconds"),
        "fetched_at": refresh.get("fetched_at"),
        "status": actual_status or ("would_submit" if would_submit else "rejected"),
        "would_submit": would_submit,
        "actual_order_status": actual_status,
        "reason": reasons[0] if reasons else "would_submit",
        "reasons": reasons,
        "arm": arm,
    }
    if target is not None:
        target["execution_parity"] = parity
    attempt_key = "|".join(
        (
            ticker,
            str(review.get("signal_matched_at") or matched_at.isoformat()),
            tier,
        )
    )
    attempt_id = hashlib.sha256(attempt_key.encode("utf-8")).hexdigest()[:24]
    attempts = ledger["live_pipeline_attempts"]
    attempt = next(
        (row for row in attempts if str(row.get("id") or "") == attempt_id),
        None,
    )
    if attempt is None:
        attempt = {
            "id": attempt_id,
            "policy_hash": ledger["configuration"]["policy_hash"],
            "captured_at": matched_at.isoformat(),
            "ticker": ticker,
            "event_ticker": (candidate or {}).get("event_ticker"),
            "close_time": (candidate or {}).get("close_time"),
            "expiry_key": (candidate or {}).get("close_time")
            or (candidate or {}).get("event_ticker"),
            "asset": (candidate or {}).get("asset"),
            "side": (candidate or {}).get("side"),
            "tier": tier,
            "settlement_status": "open" if arm else "not_applicable",
            "market_result": None,
            "settled_at": None,
        }
        attempts.append(attempt)
    attempt["execution_parity"] = parity
    attempt["execution_status"] = parity["status"]
    attempt["settlement_status"] = "open" if arm else "not_applicable"
    attempt["updated_at"] = _iso(now)
    if len(attempts) > ledger["configuration"]["maximum_records"]:
        attempts[:] = attempts[-ledger["configuration"]["maximum_records"] :]
    ledger["updated_at"] = _iso(now)
    return target or attempt


def _venue(candidate, name):
    value = (candidate.get("microstructure") or {}).get(name) or {}
    return value if isinstance(value, dict) else {}


def _common_quality(candidate, config):
    reasons = []
    if candidate.get("market_lane") != "crypto_15m" or not candidate.get(
        "is_15m_market"
    ):
        reasons.append("unsupported_market_lane")
    if str(candidate.get("asset") or "").upper() not in config["assets"]:
        reasons.append("unsupported_asset")
    minutes = _number(candidate.get("minutes_to_close"), -1.0)
    if not config["minimum_minutes"] <= minutes <= config["maximum_minutes"]:
        reasons.append("outside_registered_time_window")
    quality = candidate.get("data_quality") or {}
    if _number(quality.get("score")) < config["minimum_data_quality"]:
        reasons.append("data_quality_below_floor")
    book = candidate.get("kalshi_microstructure") or {}
    if not book.get("sequence_valid"):
        reasons.append("kalshi_sequence_invalid")
    if not book.get("fresh"):
        reasons.append("kalshi_book_stale")
    if not book.get("book_consistent"):
        reasons.append("kalshi_book_inconsistent")
    if _number(book.get("age_seconds"), 999.0) > config[
        "maximum_quote_age_seconds"
    ]:
        reasons.append("kalshi_quote_too_old")
    if _number(book.get("spread_yes_cents"), 999.0) > config[
        "maximum_spread_cents"
    ]:
        reasons.append("kalshi_spread_too_wide")
    for source in ("coinbase", "kraken"):
        venue = _venue(candidate, source)
        if not venue.get("connected"):
            reasons.append(f"{source}_unavailable")
        if _number(
            venue.get("stream_age_seconds", venue.get("book_age_seconds")), 999.0
        ) > config["maximum_source_age_seconds"]:
            reasons.append(f"{source}_stale")
    if not (candidate.get("fee_schedule") or {}).get("authoritative"):
        reasons.append("fee_schedule_not_exact")
    return {"ok": not reasons, "reasons": reasons}


def _same_direction(first, second, minimum_abs):
    first_sign = _sign(first, minimum_abs)
    second_sign = _sign(second, minimum_abs)
    return first_sign if first_sign and first_sign == second_sign else 0


def _signal_reviews(candidate):
    cb = _venue(candidate, "coinbase")
    kr = _venue(candidate, "kraken")
    kalshi = candidate.get("kalshi_microstructure") or {}
    signals = {}

    cb_flow = _number(cb.get("trade_flow_60s"))
    kr_flow = _number(kr.get("trade_flow_60s"))
    direction = _same_direction(cb_flow, kr_flow, 0.15)
    if (
        direction
        and _integer(cb.get("trade_count_60s")) >= 20
        and _integer(kr.get("trade_count_60s")) >= 20
    ):
        signals["cross_venue_flow_follow"] = {
            "side": _side(direction),
            "strength": round((abs(cb_flow) + abs(kr_flow)) / 2.0, 6),
            "evidence": {"coinbase_flow_60s": cb_flow, "kraken_flow_60s": kr_flow},
        }

    cb_return_60 = _number(cb.get("mid_return_60s_bps"))
    kr_return_60 = _number(kr.get("mid_return_60s_bps"))
    direction = _same_direction(cb_return_60, kr_return_60, 4.0)
    cb_return_15 = _number(cb.get("mid_return_15s_bps"))
    kr_return_15 = _number(kr.get("mid_return_15s_bps"))
    if (
        direction
        and _sign(cb_return_15) in {0, direction}
        and _sign(kr_return_15) in {0, direction}
        and (abs(cb_return_60) + abs(kr_return_60)) / 2.0 >= 6.0
    ):
        signals["cross_venue_impulse_follow"] = {
            "side": _side(direction),
            "strength": round((abs(cb_return_60) + abs(kr_return_60)) / 2.0, 6),
            "evidence": {
                "coinbase_return_60s_bps": cb_return_60,
                "kraken_return_60s_bps": kr_return_60,
                "coinbase_return_15s_bps": cb_return_15,
                "kraken_return_15s_bps": kr_return_15,
            },
        }

    external_return = (cb_return_60 + kr_return_60) / 2.0
    direction = _same_direction(cb_return_60, kr_return_60, 5.0)
    kalshi_move_60 = _number(kalshi.get("yes_mid_change_60s_pp"))
    if (
        direction
        and abs(external_return) >= 8.0
        and direction * kalshi_move_60 <= 0.75
    ):
        signals["spot_leads_kalshi"] = {
            "side": _side(direction),
            "strength": round(abs(external_return), 6),
            "evidence": {
                "external_return_60s_bps": external_return,
                "kalshi_yes_change_60s_cents": kalshi_move_60,
            },
        }

    cb_imbalance = _number(cb.get("top_imbalance"))
    kr_imbalance = _number(kr.get("top_imbalance"))
    direction = _same_direction(cb_imbalance, kr_imbalance, 0.15)
    cb_displacement = _number(cb.get("microprice_displacement_bps"))
    kr_displacement = _number(kr.get("microprice_displacement_bps"))
    if (
        direction
        and _sign(cb_displacement) == direction
        and _sign(kr_displacement) == direction
        and (abs(cb_displacement) + abs(kr_displacement)) / 2.0 >= 0.10
    ):
        signals["cross_venue_book_pressure"] = {
            "side": _side(direction),
            "strength": round((abs(cb_imbalance) + abs(kr_imbalance)) / 2.0, 6),
            "evidence": {
                "coinbase_top_imbalance": cb_imbalance,
                "kraken_top_imbalance": kr_imbalance,
                "coinbase_microprice_displacement_bps": cb_displacement,
                "kraken_microprice_displacement_bps": kr_displacement,
            },
        }

    kalshi_flow = _number(kalshi.get("trade_flow_60s"))
    venue_flow = (cb_flow + kr_flow) / 2.0
    direction = _sign(kalshi_flow, 0.20)
    if (
        direction
        and _sign(venue_flow, 0.12) == direction
        and _integer(kalshi.get("trade_count_60s")) >= 3
    ):
        signals["kalshi_trade_alignment"] = {
            "side": _side(direction),
            "strength": round(abs(kalshi_flow), 6),
            "evidence": {
                "kalshi_trade_flow_60s": kalshi_flow,
                "cross_venue_flow_60s": venue_flow,
                "kalshi_trade_count_60s": _integer(kalshi.get("trade_count_60s")),
            },
        }

    kalshi_move_30 = _number(kalshi.get("yes_mid_change_30s_pp"))
    external_return_30 = (
        _number(cb.get("mid_return_30s_bps"))
        + _number(kr.get("mid_return_30s_bps"))
    ) / 2.0
    direction = _sign(kalshi_move_30, 3.0)
    if direction and (
        abs(external_return_30) <= 2.0 or _sign(external_return_30) == -direction
    ):
        signals["kalshi_overreaction_fade"] = {
            "side": _side(-direction),
            "strength": round(abs(kalshi_move_30), 6),
            "evidence": {
                "kalshi_yes_change_30s_cents": kalshi_move_30,
                "external_return_30s_bps": external_return_30,
            },
        }

    ensemble_inputs = [
        value for key, value in signals.items() if key != "kalshi_overreaction_fade"
    ]
    side_counts = Counter(value["side"] for value in ensemble_inputs)
    if side_counts:
        consensus_side, agreement_count = side_counts.most_common(1)[0]
        opposing_count = sum(side_counts.values()) - agreement_count
        if agreement_count >= 2 and opposing_count == 0:
            signals[CONSENSUS_LANE] = {
                "side": consensus_side,
                "strength": float(agreement_count),
                "agreement_count": agreement_count,
                "evidence": {
                    "agreeing_lanes": sorted(
                        key
                        for key, value in signals.items()
                        if key != "kalshi_overreaction_fade"
                        and value.get("side") == consensus_side
                    ),
                    "opposing_count": opposing_count,
                },
            }
    return signals


def signal_reviews(candidate):
    """Return the preregistered signal reviews for execution-safe reuse.

    The live pilot imports this public wrapper so its signal definition cannot
    silently drift from the shadow tournament that supplied the evidence.
    """
    return _signal_reviews(candidate)


def common_quality_review(candidate, settings=None):
    """Apply the tournament's registered input-quality policy to a candidate."""
    return _common_quality(candidate, configuration(settings))


def entry_price(candidate, side):
    """Expose the registered executable entry-price lookup without duplicating it."""
    return _entry_price(candidate, side)


def _entry_price(candidate, side):
    book = candidate.get("kalshi_microstructure") or {}
    value = book.get(f"best_{side}_entry_price_cents")
    if value is None:
        value = candidate.get(f"market_{side}_ask")
    if value is None:
        value = candidate.get(f"{side}_ask")
    return _number(value, -1.0)


def _arm(candidate, side, contracts=1):
    contracts = max(1, _integer(contracts, 1))
    price = _entry_price(candidate, side)
    schedule = candidate.get("fee_schedule") or {}
    fee = kalshi_order_fee(
        price,
        contracts,
        schedule=schedule,
        liquidity_role="taker",
    )
    fee_dollars = _number(fee.get("fee_dollars"))
    return {
        "side": side,
        "contracts": contracts,
        "entry_price_cents": round(price, 6),
        "fee_dollars": round(fee_dollars, 6),
        "fee_schedule": fee,
        "total_cost_dollars": round(contracts * price / 100.0 + fee_dollars, 6),
        "result": None,
        "virtual_profit": None,
    }


def capture_candidates(ledger, candidates, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    captured_at = _iso(now)
    known = {str(row.get("id") or "") for row in ledger["records"]}
    added = []
    funnel = Counter(reviewed=0, quality_eligible=0, signal_instances=0, captured=0)
    for candidate in candidates or []:
        funnel["reviewed"] += 1
        quality = _common_quality(candidate, config)
        if not quality["ok"]:
            for reason in quality["reasons"]:
                funnel[f"reason:{reason}"] += 1
            continue
        funnel["quality_eligible"] += 1
        signals = _signal_reviews(candidate)
        if not signals:
            funnel["reason:no_registered_signal"] += 1
            continue
        spot_signal = signals.get("spot_leads_kalshi") or {}
        flow_signal = signals.get("cross_venue_flow_follow") or {}
        if (
            spot_signal
            and flow_signal
            and spot_signal.get("side") == flow_signal.get("side")
        ):
            # This dedicated audit row is the only tournament-derived record
            # that represents the live pilot's two signals on one frozen scan.
            # It is not an eighth tournament hypothesis and is excluded from
            # the seven-lane rankings below.
            signals = dict(signals)
            signals[CONTEMPORANEOUS_SPOT_FLOW_LANE] = {
                "side": spot_signal.get("side"),
                "strength": min(
                    _number(spot_signal.get("strength")),
                    _number(flow_signal.get("strength")),
                ),
                "evidence": {
                    "spot": spot_signal.get("evidence") or {},
                    "flow": flow_signal.get("evidence") or {},
                    "capture_rule": "same_candidate_same_scan",
                },
            }
        ticker = str(candidate.get("ticker") or "")
        for lane, signal in signals.items():
            funnel["signal_instances"] += 1
            record_id = f"{config['policy_hash']}:{lane}:{ticker}"
            if record_id in known:
                funnel["already_tracked"] += 1
                continue
            side = signal["side"]
            opposite = "no" if side == "yes" else "yes"
            price = _entry_price(candidate, side)
            opposite_price = _entry_price(candidate, opposite)
            if not (
                config["minimum_price_cents"]
                <= price
                <= config["maximum_price_cents"]
                and 0 < opposite_price < 100
            ):
                funnel["reason:signal_price_outside_registered_band"] += 1
                continue
            primary = _arm(candidate, side, 1)
            comparator = _arm(candidate, opposite, 1)
            if not (
                primary["fee_schedule"].get("exact")
                and comparator["fee_schedule"].get("exact")
            ):
                funnel["reason:fee_calculation_not_exact"] += 1
                continue
            agreement = max(1, _integer(signal.get("agreement_count"), 1))
            sized = None
            if lane == CONSENSUS_LANE:
                sized = {
                    "primary": _arm(candidate, side, min(5, agreement)),
                    "comparator": _arm(candidate, opposite, min(5, agreement)),
                    "sizing_rule": "one_contract_per_agreeing_signal_max_5",
                    "agreement_count": agreement,
                    "profit_delta": None,
                }
            row = {
                "id": record_id,
                "version": VERSION,
                "policy_hash": config["policy_hash"],
                "mode": MODE,
                "affects_execution": False,
                "automatic_promotion": False,
                "historical_backfill": False,
                "status": "open",
                "lane": lane,
                "captured_at": captured_at,
                "capture_batch_id": captured_at,
                "execution_validation_record": (
                    lane == CONTEMPORANEOUS_SPOT_FLOW_LANE
                ),
                "ticker": ticker,
                "event_ticker": candidate.get("event_ticker"),
                "series_ticker": candidate.get("series_ticker"),
                "asset": str(candidate.get("asset") or "").upper(),
                "close_time": candidate.get("close_time"),
                "expiry_key": candidate.get("close_time"),
                "minutes_to_close": candidate.get("minutes_to_close"),
                "signal_strength": signal.get("strength"),
                "signal_evidence": signal.get("evidence"),
                "data_quality": candidate.get("data_quality"),
                "kalshi_book_identity": {
                    key: (candidate.get("kalshi_microstructure") or {}).get(key)
                    for key in (
                        "source",
                        "sequence_valid",
                        "age_seconds",
                        "spread_yes_cents",
                        "best_yes_entry_price_cents",
                        "best_no_entry_price_cents",
                    )
                },
                "primary": primary,
                "comparator": comparator,
                "consensus_sized": sized,
                "market_result": None,
                "profit_delta": None,
            }
            ledger["records"].append(row)
            known.add(record_id)
            added.append(row)
            funnel["captured"] += 1
    retain_records(ledger, config["maximum_records"])
    ledger["last_capture_funnel"] = dict(funnel)
    ledger["last_capture_ids"] = [row["id"] for row in added]
    ledger["updated_at"] = captured_at
    return added


def _market_result(payload):
    market = (payload or {}).get("market") if isinstance(payload, dict) else None
    market = market if isinstance(market, dict) else (payload or {})
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()
    return (
        status == "finalized" and result in {"yes", "no"},
        result,
        market.get("settlement_ts") or market.get("settled_time"),
    )


def _settle_arm(arm, result):
    won = arm.get("side") == result
    payout = _integer(arm.get("contracts")) if won else 0.0
    arm.update(
        {
            "won": won,
            "result": "WIN" if won else "LOSS",
            "virtual_profit": round(
                payout - _number(arm.get("total_cost_dollars")), 6
            ),
        }
    )


def settle_records(ledger, fetch_market, settings=None, now=None):
    ledger = normalize_ledger(ledger, settings, now)
    config = ledger["configuration"]
    current = _now(now)
    cache = {}
    settled = []
    checks = 0
    for row in ledger["records"]:
        if row.get("status") != "open":
            continue
        if checks >= config["maximum_settlement_checks_per_scan"]:
            break
        try:
            close = _now(row.get("close_time"))
        except (TypeError, ValueError):
            continue
        if current < close + timedelta(minutes=config["settlement_grace_minutes"]):
            continue
        last_check = row.get("last_settlement_check_at")
        if last_check:
            try:
                if current < _now(last_check) + timedelta(
                    minutes=config["settlement_retry_minutes"]
                ):
                    continue
            except (TypeError, ValueError):
                pass
        ticker = str(row.get("ticker") or "")
        if ticker not in cache:
            try:
                cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                cache[ticker] = {"status": "error", "error": type(exc).__name__}
            checks += 1
        finalized, result, settled_at = _market_result(cache[ticker])
        row["last_settlement_check_at"] = current.isoformat()
        row["settlement_checks"] = _integer(row.get("settlement_checks")) + 1
        if not finalized:
            continue
        _settle_arm(row["primary"], result)
        _settle_arm(row["comparator"], result)
        parity = row.get("execution_parity") or {}
        if parity.get("arm"):
            _settle_arm(parity["arm"], result)
            parity["market_result"] = result
            parity["settled_at"] = settled_at or current.isoformat()
        row["profit_delta"] = round(
            _number(row["primary"].get("virtual_profit"))
            - _number(row["comparator"].get("virtual_profit")),
            6,
        )
        sized = row.get("consensus_sized") or {}
        if sized.get("primary") and sized.get("comparator"):
            _settle_arm(sized["primary"], result)
            _settle_arm(sized["comparator"], result)
            sized["profit_delta"] = round(
                _number(sized["primary"].get("virtual_profit"))
                - _number(sized["comparator"].get("virtual_profit")),
                6,
            )
        row.update(
            {
                "status": "settled",
                "market_result": result,
                "settled_at": settled_at or current.isoformat(),
            }
        )
        settled.append(row)
    for attempt in ledger.get("live_pipeline_attempts") or []:
        if attempt.get("settlement_status") != "open":
            continue
        if checks >= config["maximum_settlement_checks_per_scan"]:
            break
        arm = (attempt.get("execution_parity") or {}).get("arm")
        if not arm:
            attempt["settlement_status"] = "not_applicable"
            continue
        try:
            close = _now(attempt.get("close_time"))
        except (TypeError, ValueError):
            continue
        if current < close + timedelta(minutes=config["settlement_grace_minutes"]):
            continue
        last_check = attempt.get("last_settlement_check_at")
        if last_check:
            try:
                if current < _now(last_check) + timedelta(
                    minutes=config["settlement_retry_minutes"]
                ):
                    continue
            except (TypeError, ValueError):
                pass
        ticker = str(attempt.get("ticker") or "")
        if ticker not in cache:
            try:
                cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                cache[ticker] = {"status": "error", "error": type(exc).__name__}
            checks += 1
        finalized, result, settled_at = _market_result(cache[ticker])
        attempt["last_settlement_check_at"] = current.isoformat()
        attempt["settlement_checks"] = _integer(attempt.get("settlement_checks")) + 1
        if not finalized:
            continue
        _settle_arm(arm, result)
        attempt["settlement_status"] = "settled"
        attempt["market_result"] = result
        attempt["settled_at"] = settled_at or current.isoformat()
        settled.append(attempt)
    ledger["last_settled_ids"] = [row["id"] for row in settled]
    if settled:
        ledger["updated_at"] = current.isoformat()
    return settled


def _arm_summary(rows, key):
    arms = [row.get(key) or {} for row in rows]
    cost = sum(_number(arm.get("total_cost_dollars")) for arm in arms)
    profit = sum(_number(arm.get("virtual_profit")) for arm in arms)
    wins = sum(arm.get("result") == "WIN" for arm in arms)
    return {
        "settled": len(rows),
        "wins": wins,
        "losses": len(rows) - wins,
        "win_rate": round(100.0 * wins / len(rows), 2) if rows else 0.0,
        "cost": round(cost, 4),
        "profit": round(profit, 4),
        "roi": round(100.0 * profit / cost, 2) if cost else 0.0,
    }


def _cluster_lower(rows, getter, cluster_getter):
    clusters = {}
    for row in rows:
        key = str(cluster_getter(row) or "")
        if not key:
            continue
        clusters[key] = clusters.get(key, 0.0) + _number(getter(row))
    values = list(clusters.values())
    if len(values) < 2:
        return {
            "lower_bound": None,
            "clusters": len(values),
            "confidence_level": round(PER_HYPOTHESIS_CONFIDENCE, 8),
        }
    mean = statistics.fmean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    return {
        "mean_cluster_profit": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "lower_bound": round(mean - FAMILY_Z * standard_error, 6),
        "clusters": len(values),
        "confidence_level": round(PER_HYPOTHESIS_CONFIDENCE, 8),
    }


def _lane_summary(records, lane, config):
    rows = [row for row in records if row.get("lane") == lane and row.get("policy_hash") == config["policy_hash"]]
    settled = sorted(
        [row for row in rows if row.get("status") == "settled"],
        key=lambda row: str(row.get("captured_at") or ""),
    )
    primary = _arm_summary(settled, "primary")
    comparator = _arm_summary(settled, "comparator")
    split = len(settled) // 2
    first = _arm_summary(settled[:split], "primary")
    second = _arm_summary(settled[split:], "primary")
    days = {
        _observation_date(row.get("captured_at"))
        for row in settled
        if _observation_date(row.get("captured_at"))
    }
    unique_markets = len({str(row.get("ticker") or "") for row in settled})
    expiry_lower = _cluster_lower(
        settled,
        lambda row: (row.get("primary") or {}).get("virtual_profit"),
        lambda row: row.get("expiry_key") or row.get("close_time"),
    )
    day_lower = _cluster_lower(
        settled,
        lambda row: (row.get("primary") or {}).get("virtual_profit"),
        lambda row: _observation_date(row.get("captured_at")),
    )
    delta_day_lower = _cluster_lower(
        settled,
        lambda row: row.get("profit_delta"),
        lambda row: _observation_date(row.get("captured_at")),
    )
    evidence_gates = {
        "profitable": primary["profit"] > 0,
        "beats_simultaneous_opposite": primary["profit"] > comparator["profit"],
        "both_chronological_halves_profitable": bool(
            first["settled"]
            and second["settled"]
            and first["profit"] > 0
            and second["profit"] > 0
        ),
        "expiry_cluster_lower_positive": expiry_lower["lower_bound"] is not None
        and expiry_lower["lower_bound"] > 0,
        "day_cluster_lower_positive": day_lower["lower_bound"] is not None
        and day_lower["lower_bound"] > 0,
        "paired_delta_day_lower_positive": delta_day_lower["lower_bound"]
        is not None
        and delta_day_lower["lower_bound"] > 0,
    }
    interim_gates = {
        "minimum_unique_markets": unique_markets
        >= config["minimum_unique_markets_interim"],
        "minimum_observation_days": len(days)
        >= config["minimum_observation_days_interim"],
        **evidence_gates,
    }
    final_gates = {
        "minimum_unique_markets": unique_markets
        >= config["minimum_unique_markets_final"],
        "minimum_observation_days": len(days)
        >= config["minimum_observation_days_final"],
        **evidence_gates,
    }
    sized = None
    if lane == CONSENSUS_LANE:
        sized_rows = [
            row
            for row in settled
            if (row.get("consensus_sized") or {}).get("primary")
        ]
        primary_arms = [
            {**row, "primary": row["consensus_sized"]["primary"]}
            for row in sized_rows
        ]
        comparator_arms = [
            {**row, "comparator": row["consensus_sized"]["comparator"]}
            for row in sized_rows
        ]
        sized = {
            "primary": _arm_summary(primary_arms, "primary"),
            "comparator": _arm_summary(comparator_arms, "comparator"),
            "profit_delta": round(
                sum(
                    _number((row.get("consensus_sized") or {}).get("profit_delta"))
                    for row in sized_rows
                ),
                4,
            ),
            "sizing_rule": "one_contract_per_agreeing_signal_max_5",
        }
    parity_rows = [row for row in rows if row.get("execution_parity")]
    parity_fillable = [
        row
        for row in parity_rows
        if (row.get("execution_parity") or {}).get("would_submit")
    ]
    parity_settled = [
        {**row, "execution_parity_arm": (row.get("execution_parity") or {}).get("arm")}
        for row in parity_fillable
        if ((row.get("execution_parity") or {}).get("arm") or {}).get("result")
        in {"WIN", "LOSS"}
    ]
    parity_cost = sum(
        _number((row.get("execution_parity_arm") or {}).get("total_cost_dollars"))
        for row in parity_settled
    )
    parity_profit = sum(
        _number((row.get("execution_parity_arm") or {}).get("virtual_profit"))
        for row in parity_settled
    )
    parity_wins = sum(
        (row.get("execution_parity_arm") or {}).get("result") == "WIN"
        for row in parity_settled
    )
    parity_reasons = Counter(
        str((row.get("execution_parity") or {}).get("reason") or "unknown")
        for row in parity_rows
        if not (row.get("execution_parity") or {}).get("would_submit")
    )
    execution_parity = {
        "tracked": len(parity_rows),
        "book_fillable": len(parity_fillable),
        "rejected": len(parity_rows) - len(parity_fillable),
        "settled": len(parity_settled),
        "wins": parity_wins,
        "losses": len(parity_settled) - parity_wins,
        "cost": round(parity_cost, 4),
        "profit": round(parity_profit, 4),
        "roi": round(100.0 * parity_profit / parity_cost, 2)
        if parity_cost
        else 0.0,
        "rejection_reasons": dict(parity_reasons.most_common()),
        "fill_model": "fresh_visible_taker_fok_depth",
        "automatic_promotion": False,
    }
    return {
        "lane": lane,
        "tracked": len(rows),
        "open": sum(row.get("status") == "open" for row in rows),
        "settled": len(settled),
        "unique_markets": unique_markets,
        "observation_days": len(days),
        "primary": primary,
        "comparator": comparator,
        "profit_delta": round(primary["profit"] - comparator["profit"], 4),
        "chronological_halves": {"first": first, "second": second},
        "expiry_cluster_inference": expiry_lower,
        "day_cluster_inference": day_lower,
        "delta_day_cluster_inference": delta_day_lower,
        "consensus_sized": sized,
        "execution_parity": execution_parity,
        "interim_review": {
            "eligible": bool(interim_gates) and all(interim_gates.values()),
            "gates": interim_gates,
            "automatic_promotion": False,
        },
        "final_review": {
            "eligible": bool(final_gates) and all(final_gates.values()),
            "gates": final_gates,
            "automatic_promotion": False,
        },
    }


def summarize(ledger, settings=None):
    ledger = normalize_ledger(ledger, settings)
    config = ledger["configuration"]
    current_records = [
        row
        for row in ledger["records"]
        if row.get("policy_hash") == config["policy_hash"]
    ]
    lanes = [_lane_summary(current_records, lane, config) for lane in LANES]
    ranked_records = [row for row in current_records if row.get("lane") in LANES]
    ranked = sorted(
        lanes,
        key=lambda row: (
            row["primary"]["profit"],
            row["profit_delta"],
            row["settled"],
        ),
        reverse=True,
    )
    parity_records = [row for row in current_records if row.get("execution_parity")]
    parity_fillable = [
        row
        for row in parity_records
        if (row.get("execution_parity") or {}).get("would_submit")
    ]
    parity_settled = [
        row
        for row in parity_fillable
        if (((row.get("execution_parity") or {}).get("arm") or {}).get("result"))
        in {"WIN", "LOSS"}
    ]
    parity_cost = sum(
        _number(
            (((row.get("execution_parity") or {}).get("arm") or {}).get(
                "total_cost_dollars"
            ))
        )
        for row in parity_settled
    )
    parity_profit = sum(
        _number(
            (((row.get("execution_parity") or {}).get("arm") or {}).get(
                "virtual_profit"
            ))
        )
        for row in parity_settled
    )
    try:
        parity_registered = _now(ledger.get("execution_parity_registered_at"))
    except (TypeError, ValueError):
        parity_registered = _now()
    live_attempts = [
        row
        for row in ledger.get("live_pipeline_attempts") or []
        if row.get("policy_hash") == config["policy_hash"]
        and _now(row.get("captured_at")) >= parity_registered
    ]
    live_settled = [
        row
        for row in live_attempts
        if row.get("settlement_status") == "settled"
        and (((row.get("execution_parity") or {}).get("arm") or {}).get("result"))
        in {"WIN", "LOSS"}
    ]
    live_cost = sum(
        _number(
            (((row.get("execution_parity") or {}).get("arm") or {}).get(
                "total_cost_dollars"
            ))
        )
        for row in live_settled
    )
    live_profit = sum(
        _number(
            (((row.get("execution_parity") or {}).get("arm") or {}).get(
                "virtual_profit"
            ))
        )
        for row in live_settled
    )
    live_reasons = Counter(
        str((row.get("execution_parity") or {}).get("reason") or "unknown")
        for row in live_attempts
        if not (row.get("execution_parity") or {}).get("would_submit")
    )
    return {
        "version": VERSION,
        "mode": MODE if config["enabled"] else "disabled",
        "affects_execution": False,
        "automatic_promotion": False,
        "historical_backfill": False,
        "registered_at": ledger.get("registered_at"),
        "updated_at": ledger.get("updated_at"),
        "execution_parity": {
            "version": EXECUTION_PARITY_VERSION,
            "registered_at": ledger.get("execution_parity_registered_at"),
            "historical_backfill": False,
            "affects_execution": False,
            "automatic_promotion": False,
            "tracked": len(parity_records),
            "book_fillable": len(parity_fillable),
            "rejected": len(parity_records) - len(parity_fillable),
            "settled": len(parity_settled),
            "profit": round(parity_profit, 4),
            "cost": round(parity_cost, 4),
            "roi": round(100.0 * parity_profit / parity_cost, 2)
            if parity_cost
            else 0.0,
            "warning": (
                "Only execution-parity rows use refreshed executable prices; "
                "raw signal-snapshot ROI is not production-fill evidence."
            ),
            "live_policy": {
                "tracked": len(live_attempts),
                "would_submit": sum(
                    bool((row.get("execution_parity") or {}).get("would_submit"))
                    for row in live_attempts
                ),
                "actual_filled": sum(
                    (row.get("execution_parity") or {}).get("actual_order_status")
                    == "filled"
                    for row in live_attempts
                ),
                "rejected": sum(
                    not bool((row.get("execution_parity") or {}).get("would_submit"))
                    for row in live_attempts
                ),
                "settled": len(live_settled),
                "profit": round(live_profit, 4),
                "cost": round(live_cost, 4),
                "roi": round(100.0 * live_profit / live_cost, 2)
                if live_cost
                else 0.0,
                "rejection_reasons": dict(live_reasons.most_common()),
                "recent_attempts": sorted(
                    live_attempts,
                    key=lambda row: str(row.get("updated_at") or row.get("captured_at") or ""),
                    reverse=True,
                )[:50],
            },
        },
        "configuration": config,
        "tracked": len(ranked_records),
        "open": sum(row.get("status") == "open" for row in ranked_records),
        "settled": sum(row.get("status") == "settled" for row in ranked_records),
        "contemporaneous_spot_flow": {
            "tracked": sum(
                row.get("lane") == CONTEMPORANEOUS_SPOT_FLOW_LANE
                for row in current_records
            ),
            "open": sum(
                row.get("lane") == CONTEMPORANEOUS_SPOT_FLOW_LANE
                and row.get("status") == "open"
                for row in current_records
            ),
            "settled": sum(
                row.get("lane") == CONTEMPORANEOUS_SPOT_FLOW_LANE
                and row.get("status") == "settled"
                for row in current_records
            ),
        },
        "lanes": lanes,
        "ranking": [
            {
                "lane": row["lane"],
                "settled": row["settled"],
                "profit": row["primary"]["profit"],
                "roi": row["primary"]["roi"],
                "profit_delta": row["profit_delta"],
                "interim_eligible": row["interim_review"]["eligible"],
                "final_eligible": row["final_review"]["eligible"],
            }
            for row in ranked
        ],
        "last_capture_funnel": dict(ledger.get("last_capture_funnel") or {}),
        "recent_records": sorted(
            current_records,
            key=lambda row: str(row.get("settled_at") or row.get("captured_at") or ""),
            reverse=True,
        )[:100],
    }
