"""BTC 15-minute CF Benchmarks settlement-lag research lane.

This module is deliberately incapable of placing orders.  It consumes the
authenticated Kalshi BRTI and order-book streams, writes an isolated shadow
ledger, and resolves hypothetical entries only from official market results.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from crypto_pricing import kalshi_order_fee, norm_cdf


VERSION = "btc-settlement-lag-shadow-v1"
MODE = "paper_shadow_only"
INTEGRITY_REVISION = "source-time-alignment-v2"
CHICAGO = ZoneInfo("America/Chicago")
ONE_SIDED_90_Z = 1.2815515655446004


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


def _boolean(value, default=False):
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _setting(settings, key, default):
    return (settings or {}).get(key, default)


def _iso(unix=None):
    return datetime.fromtimestamp(
        time.time() if unix is None else float(unix), timezone.utc
    ).isoformat()


def _parse_time(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _observation_date(value):
    parsed = _parse_time(value)
    return parsed.astimezone(CHICAGO).date().isoformat() if parsed else None


def expected_window_reading(source_ts_ms, close_time):
    """Return the official 1..60 BRTI reading implied by source time."""
    close = _parse_time(close_time)
    source = _number(source_ts_ms)
    if not close or source <= 0:
        return None
    expected_float = (source - (close.timestamp() * 1000.0 - 60000.0)) / 1000.0
    expected = int(round(expected_float))
    if abs(expected_float - expected) > 0.25 or not 1 <= expected <= 60:
        return None
    return expected


def configuration(settings=None):
    thresholds = []
    for value in str(
        _setting(settings, "CRYPTO_SETTLEMENT_LAG_EDGE_THRESHOLDS_CENTS", "4,6,8")
    ).split(","):
        try:
            threshold = float(value.strip())
        except ValueError:
            continue
        if threshold > 0:
            thresholds.append(round(threshold, 2))
    thresholds = sorted(set(thresholds)) or [4.0, 6.0, 8.0]
    observation_readings = []
    for value in str(
        _setting(settings, "CRYPTO_SETTLEMENT_LAG_OBSERVATION_READINGS", "25,35,45")
    ).split(","):
        try:
            reading = int(float(value.strip()))
        except ValueError:
            continue
        if 1 <= reading <= 59:
            observation_readings.append(reading)
    observation_readings = sorted(set(observation_readings)) or [25, 35, 45]
    return {
        "enabled": _boolean(
            _setting(settings, "CRYPTO_SETTLEMENT_LAG_SHADOW_ENABLED", True), True
        ),
        "index_id": str(
            _setting(settings, "CRYPTO_SETTLEMENT_LAG_INDEX_ID", "BRTI")
        ).strip().upper(),
        "minimum_reading": max(
            1, _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MIN_READING", 25), 25)
        ),
        "maximum_reading": min(
            59, _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MAX_READING", 52), 52)
        ),
        "edge_thresholds_cents": thresholds,
        "observation_readings": observation_readings,
        "virtual_contracts": max(
            1, _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_VIRTUAL_CONTRACTS", 10), 10)
        ),
        "minimum_depth_contracts": max(
            1.0,
            _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MIN_DEPTH_CONTRACTS", 10), 10),
        ),
        "maximum_spread_cents": max(
            0.0, _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MAX_SPREAD_CENTS", 4), 4)
        ),
        "maximum_quote_age_seconds": max(
            0.1,
            _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MAX_QUOTE_AGE_SECONDS", 2), 2),
        ),
        "fok_reconfirmation_max_delay_seconds": max(
            0.1,
            _number(
                _setting(
                    settings,
                    "CRYPTO_SETTLEMENT_LAG_FOK_RECONFIRM_MAX_DELAY_SECONDS",
                    2.5,
                ),
                2.5,
            ),
        ),
        "volatility_lookback_seconds": max(
            30.0,
            _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_VOL_LOOKBACK_SECONDS", 300), 300),
        ),
        "minimum_empirical_samples": max(
            10,
            _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MIN_EMPIRICAL_SAMPLES", 30), 30),
        ),
        "probability_cap_pct": min(
            99.5,
            max(50.0, _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_PROBABILITY_CAP_PCT", 98), 98)),
        ),
        "settlement_grace_minutes": max(
            0.0,
            _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_SETTLEMENT_GRACE_MINUTES", 2), 2),
        ),
        "settlement_retry_seconds": max(
            15.0,
            _number(_setting(settings, "CRYPTO_SETTLEMENT_LAG_SETTLEMENT_RETRY_SECONDS", 60), 60),
        ),
        "max_records": max(
            100, _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MAX_RECORDS", 5000), 5000)
        ),
        "minimum_independent_markets": max(
            30,
            _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MIN_INDEPENDENT_MARKETS", 200), 200),
        ),
        "minimum_observation_days": max(
            1, _integer(_setting(settings, "CRYPTO_SETTLEMENT_LAG_MIN_OBSERVATION_DAYS", 30), 30)
        ),
        "automatic_promotion": False,
        "affects_execution": False,
    }


def empty_ledger(settings=None):
    return {
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "automatic_promotion": False,
        "created_at": _iso(),
        "updated_at": None,
        "configuration": configuration(settings),
        "records": [],
        "observations": [],
        "observation_dates": [],
        "windows": {},
        "calibration_samples": {},
        "last_tick_review": {},
        "health": {"status": "waiting_for_stream"},
        "summary": {},
    }


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, payload):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    last_error = None
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    if last_error:
        raise last_error


def normalize_ledger(payload, settings=None):
    base = empty_ledger(settings)
    if isinstance(payload, dict) and payload.get("version") == VERSION:
        base.update(payload)
    base["configuration"] = configuration(settings)
    base["mode"] = MODE
    base["affects_execution"] = False
    base["automatic_promotion"] = False
    base["integrity_revision"] = INTEGRITY_REVISION
    base["records"] = list(base.get("records") or [])
    base["observations"] = list(base.get("observations") or [])
    base["windows"] = dict(base.get("windows") or {})
    recorded_dates = {
        str(value)
        for value in (base.get("observation_dates") or [])
        if str(value or "").strip()
    }
    for collection in (
        base["windows"].values(),
        base["observations"],
        base["records"],
    ):
        for row in collection:
            day = _observation_date(row.get("close_time") or row.get("captured_at"))
            if day:
                recorded_dates.add(day)
    base["observation_dates"] = sorted(recorded_dates)
    base["calibration_samples"] = {
        str(key): list(values or [])[-2000:]
        for key, values in dict(base.get("calibration_samples") or {}).items()
    }
    invalidated_at = _iso()
    for row in base["records"]:
        expected = expected_window_reading(row.get("source_ts_ms"), row.get("close_time"))
        actual = _integer(row.get("reading"))
        aligned = expected is not None and actual == expected
        row["expected_reading_from_source_time"] = expected
        row["source_time_aligned"] = aligned
        if not aligned:
            row["valid_for_evaluation"] = False
            row["evaluation_status"] = "invalid_window_time_alignment"
            row.setdefault("invalidated_at", invalidated_at)
        else:
            row.setdefault("valid_for_evaluation", True)
            row.setdefault("evaluation_status", "valid_time_aligned")
    valid_by_ticker = {}
    for row in base["records"]:
        row["market_primary"] = False
        if row.get("valid_for_evaluation", True):
            valid_by_ticker.setdefault(str(row.get("ticker") or ""), []).append(row)
    for rows in valid_by_ticker.values():
        primary = min(
            rows,
            key=lambda row: (
                str(row.get("captured_at") or ""),
                _number(row.get("edge_threshold_cents"), 999.0),
                str(row.get("id") or ""),
            ),
        )
        primary["market_primary"] = True
    for row in base["observations"]:
        expected = expected_window_reading(row.get("source_ts_ms"), row.get("close_time"))
        row["expected_reading_from_source_time"] = expected
        row["source_time_aligned"] = expected is not None and _integer(row.get("reading")) == expected
        row["valid_for_evaluation"] = bool(
            row.get("source_time_aligned")
            and row.get("window_integrity", True)
            and row.get("sequence_valid", True)
        )
    return base


def required_remaining_average(target, observed_average, observed_count, total=60):
    count = max(0, min(int(total), int(observed_count)))
    remaining = int(total) - count
    if remaining <= 0:
        return None
    return (
        float(total) * float(target) - count * float(observed_average)
    ) / remaining


def _remaining_bucket(remaining):
    remaining = max(1, int(remaining))
    low = ((remaining - 1) // 5) * 5 + 1
    high = low + 4
    return f"{low:02d}-{high:02d}"


def _robust_tick_sigma(history, current_value):
    values = [
        _number(row.get("value"))
        for row in history or []
        if _number(row.get("value")) > 0
    ]
    differences = [values[index] - values[index - 1] for index in range(1, len(values))]
    floor = max(0.01, float(current_value) * 0.000002)
    if len(differences) < 5:
        return floor, differences[-1] if differences else 0.0, len(differences)
    center = statistics.median(differences)
    mad = statistics.median(abs(value - center) for value in differences)
    robust = max(floor, 1.4826 * mad)
    return robust, differences[-1], len(differences)


def _future_average_scale(tick_sigma, remaining):
    remaining = max(1, int(remaining))
    factor = math.sqrt(
        ((remaining + 1.0) * (2.0 * remaining + 1.0)) / (6.0 * remaining)
    )
    return max(1e-9, float(tick_sigma) * factor)


def settlement_probability(
    *,
    target,
    observed_average,
    observed_count,
    current_value,
    tick_sigma,
    calibration_samples=None,
    market_kind="above",
    probability_cap_pct=98.0,
    minimum_empirical_samples=30,
):
    count = max(0, min(60, int(observed_count)))
    remaining = 60 - count
    required = required_remaining_average(target, observed_average, count)
    if remaining <= 0 or required is None:
        above = 1.0 if float(observed_average) >= float(target) else 0.0
        low = high = above
        method = "final_window_deterministic"
        samples = 0
        scale = 0.0
        z_score = None
    else:
        scale = _future_average_scale(tick_sigma, remaining)
        z_score = (float(required) - float(current_value)) / scale
        # A small wide-normal component makes the fallback materially more
        # conservative than a Gaussian during short-lived BTC jumps.
        fallback_above = (
            0.90 * (1.0 - norm_cdf(z_score))
            + 0.10 * (1.0 - norm_cdf(z_score / 3.0))
        )
        samples_for_bucket = list(
            (calibration_samples or {}).get(_remaining_bucket(remaining)) or []
        )
        samples = len(samples_for_bucket)
        if samples:
            empirical_above = (
                sum(1 for value in samples_for_bucket if _number(value) >= z_score) + 0.5
            ) / (samples + 1.0)
            empirical_weight = min(0.75, samples / (samples + 60.0))
            above = (
                empirical_weight * empirical_above
                + (1.0 - empirical_weight) * fallback_above
            )
        else:
            above = fallback_above
        cap = max(0.50, min(0.995, float(probability_cap_pct) / 100.0))
        above = max(1.0 - cap, min(cap, above))
        base_width = 0.08 if samples < minimum_empirical_samples else 0.04
        sample_width = 0.18 / math.sqrt(max(1.0, samples)) if samples else 0.08
        width = max(0.025, min(0.12, base_width + sample_width))
        low = max(0.0, above - width)
        high = min(1.0, above + width)
        method = (
            "empirical_heavy_tail_blend"
            if samples >= minimum_empirical_samples
            else "heavy_tail_collecting"
        )
    kind = str(market_kind or "above").lower()
    if kind == "below":
        p_yes = 1.0 - above
        p_low = 1.0 - high
        p_high = 1.0 - low
    else:
        p_yes, p_low, p_high = above, low, high
    return {
        "p_yes": round(p_yes * 100.0, 4),
        "p_low": round(p_low * 100.0, 4),
        "p_high": round(p_high * 100.0, 4),
        "required_remaining_average": round(required, 8) if required is not None else None,
        "remaining_readings": remaining,
        "future_average_sigma": round(scale, 8),
        "required_distance_sigma": round(z_score, 5) if z_score is not None else None,
        "tick_sigma": round(float(tick_sigma), 8),
        "calibration_samples": samples,
        "calibration_bucket": _remaining_bucket(remaining) if remaining else "final",
        "method": method,
    }


def _book_levels(book, side):
    return [
        (float(price), float(size))
        for price, size, *_ in (book.get(f"{side}_levels") or [])
        if 0 < _number(price) < 100 and _number(size) > 0
    ]


def _walk_levels(levels, contracts):
    needed = float(contracts)
    filled = 0.0
    notional = 0.0
    fills = []
    for price, size in levels:
        take = min(max(0.0, size), needed - filled)
        if take <= 0:
            continue
        filled += take
        notional += take * price
        fills.append({"price_cents": round(price, 6), "contracts": round(take, 6)})
        if filled + 1e-9 >= needed:
            break
    return {
        "filled_contracts": round(filled, 6),
        "vwap_cents": round(notional / filled, 6) if filled else None,
        "full_fill": filled + 1e-9 >= needed,
        "fills": fills,
    }


def _execution_fee(execution, schedule):
    details = [
        kalshi_order_fee(
            fill.get("price_cents"),
            fill.get("contracts"),
            schedule=schedule or {},
            liquidity_role="taker",
        )
        for fill in (execution.get("fills") or [])
    ]
    contracts = sum(_number(row.get("contracts")) for row in details)
    total = sum(_number(row.get("fee_dollars")) for row in details)
    return {
        "exact": bool(details) and all(row.get("exact") for row in details),
        "fee_dollars": round(total, 6),
        "fee_per_contract_dollars": round(total / contracts, 8) if contracts else 0.0,
        "contracts": round(contracts, 6),
        "fill_fee_details": details,
        "fee_type": details[0].get("fee_type") if details else "unknown",
        "fee_multiplier": details[0].get("fee_multiplier") if details else None,
        "source": details[0].get("source") if details else "unavailable",
        "series_ticker": details[0].get("series_ticker") if details else "",
    }


def executable_entry(book, side, contracts):
    side = str(side or "yes").lower()
    contra = "no" if side == "yes" else "yes"
    asks = sorted((100.0 - price, size) for price, size in _book_levels(book, contra))
    bids = sorted(_book_levels(book, side), reverse=True)
    walked = _walk_levels(asks, contracts)
    best_ask = asks[0][0] if asks else None
    best_bid = bids[0][0] if bids else None
    return {
        **walked,
        "best_ask_cents": round(best_ask, 6) if best_ask is not None else None,
        "best_bid_cents": round(best_bid, 6) if best_bid is not None else None,
        "spread_cents": (
            round(max(0.0, best_ask - best_bid), 6)
            if best_ask is not None and best_bid is not None
            else None
        ),
        "book_walk_slippage_cents": (
            round(max(0.0, _number(walked.get("vwap_cents")) - best_ask), 6)
            if best_ask is not None and walked.get("vwap_cents") is not None
            else None
        ),
        "available_contracts": round(sum(size for _price, size in asks), 6),
    }


def executable_exit(book, side, contracts):
    bids = sorted(_book_levels(book, str(side or "yes").lower()), reverse=True)
    walked = _walk_levels(bids, contracts)
    return {
        **walked,
        "best_bid_cents": round(bids[0][0], 6) if bids else None,
        "available_contracts": round(sum(size for _price, size in bids), 6),
    }


def _selected_probability(probability, side):
    if side == "yes":
        return (
            _number(probability.get("p_yes")),
            _number(probability.get("p_low")),
            _number(probability.get("p_high")),
        )
    return (
        100.0 - _number(probability.get("p_yes")),
        100.0 - _number(probability.get("p_high")),
        100.0 - _number(probability.get("p_low")),
    )


def _entry_band(reading):
    reading = int(reading)
    if 25 <= reading <= 34:
        return "25-34"
    if 35 <= reading <= 44:
        return "35-44"
    if 45 <= reading <= 52:
        return "45-52"
    return "outside"


def _market_result(payload):
    payload = payload or {}
    status = str(payload.get("status") or "").strip().lower()
    result = str(payload.get("result") or payload.get("settlement_value") or "").strip().lower()
    if result in {"1", "yes", "y", "true"}:
        result = "yes"
    elif result in {"0", "no", "n", "false"}:
        result = "no"
    else:
        result = ""
    return (
        status == "finalized" and result in {"yes", "no"},
        result,
        payload.get("settlement_ts") or payload.get("settled_time"),
    )


def _finalize_calibration_window(ledger, window):
    if (
        window.get("calibration_finalized")
        and window.get("calibration_status") == "complete"
    ):
        return
    ticks = {
        int(key): value for key, value in dict(window.get("ticks") or {}).items()
    }
    if not all(bool(row.get("source_time_aligned")) for row in ticks.values()):
        window["calibration_status"] = "invalid_window_time_alignment"
        window["calibration_finalized"] = True
        return
    if not all(index in ticks for index in range(1, 61)):
        window["calibration_status"] = "incomplete_window"
        window["calibration_finalized"] = True
        return
    for count in range(25, 53):
        current = _number(ticks[count].get("value"))
        sigma = _number(ticks[count].get("tick_sigma"))
        future = [_number(ticks[index].get("value")) for index in range(count + 1, 61)]
        if current <= 0 or sigma <= 0 or not future:
            continue
        scale = _future_average_scale(sigma, len(future))
        residual = (sum(future) / len(future) - current) / scale
        bucket = _remaining_bucket(len(future))
        values = ledger.setdefault("calibration_samples", {}).setdefault(bucket, [])
        values.append(round(residual, 8))
        del values[:-2000]
    window["calibration_status"] = "complete"
    window["calibration_finalized"] = True


def _evaluate_opportunity(ledger, market, cf, history, book, config):
    reading = _integer(cf.get("window_size"))
    window_close_ms = _number(cf.get("window_close_ts_ms"))
    window_close = (
        datetime.fromtimestamp(window_close_ms / 1000.0, timezone.utc)
        if window_close_ms > 0
        else market.get("close_time")
    )
    expected_reading = expected_window_reading(cf.get("source_ts_ms"), window_close)
    source_time_aligned = bool(
        cf.get("window_time_aligned", True)
        and expected_reading is not None
        and reading == expected_reading
    )
    target = _number(market.get("target"))
    observed_average = _number(cf.get("window_average"))
    current_value = _number(cf.get("value"))
    tick_sigma, latest_move, volatility_samples = _robust_tick_sigma(history, current_value)
    probability = settlement_probability(
        target=target,
        observed_average=observed_average,
        observed_count=reading,
        current_value=current_value,
        tick_sigma=tick_sigma,
        calibration_samples=ledger.get("calibration_samples") or {},
        market_kind=market.get("market_kind") or "above",
        probability_cap_pct=config["probability_cap_pct"],
        minimum_empirical_samples=config["minimum_empirical_samples"],
    )
    contracts = config["virtual_contracts"]
    sides = []
    for side in ("yes", "no"):
        execution = executable_entry(book, side, contracts)
        if not execution.get("full_fill") or execution.get("vwap_cents") is None:
            continue
        selected, selected_low, selected_high = _selected_probability(probability, side)
        fee = _execution_fee(execution, market.get("fee_schedule") or {})
        fee_cents = _number(fee.get("fee_per_contract_dollars")) * 100.0
        expected_edge = selected - execution["vwap_cents"] - fee_cents
        edge_low = selected_low - execution["vwap_cents"] - fee_cents
        sides.append({
            "side": side,
            "selected_probability": round(selected, 4),
            "selected_probability_low": round(selected_low, 4),
            "selected_probability_high": round(selected_high, 4),
            "expected_edge_cents": round(expected_edge, 4),
            "edge_low_cents": round(edge_low, 4),
            "execution": execution,
            "fee": fee,
            "exact_fee_cents": round(fee_cents, 6),
        })
    selected = max(sides, key=lambda row: row["expected_edge_cents"], default={})
    reasons = []
    if not cf.get("sequence_valid", True):
        reasons.append("brti_sequence_gap")
    if not cf.get("window_integrity", True):
        reasons.append("brti_window_incomplete")
    if not source_time_aligned:
        reasons.append("brti_window_time_mismatch")
    if not cf.get("fresh"):
        reasons.append("brti_stale")
    if not book.get("fresh") or not book.get("sequence_valid", True):
        reasons.append("orderbook_stale_or_invalid")
    if reading < config["minimum_reading"]:
        reasons.append("entry_window_not_started")
    if reading > config["maximum_reading"]:
        reasons.append("entry_window_closed")
    if not selected:
        reasons.append("insufficient_executable_depth")
    else:
        execution = selected.get("execution") or {}
        if _number(execution.get("available_contracts")) < config["minimum_depth_contracts"]:
            reasons.append("insufficient_executable_depth")
        if execution.get("spread_cents") is None:
            reasons.append("two_sided_book_unavailable")
        elif _number(execution.get("spread_cents")) > config["maximum_spread_cents"]:
            reasons.append("spread_too_wide")
        if not (selected.get("fee") or {}).get("exact"):
            reasons.append("exact_fee_unavailable")
        if _number(selected.get("edge_low_cents")) <= 0:
            reasons.append("conservative_edge_not_positive")
    spike_limit = max(tick_sigma * 8.0, current_value * 0.0005)
    if abs(latest_move) > spike_limit:
        reasons.append("violent_brti_spike")
    return {
        "reviewed_at": _iso(),
        "ticker": market.get("ticker"),
        "close_time": market.get("close_time"),
        "target": target,
        "market_kind": market.get("market_kind") or "above",
        "reading": reading,
        "expected_reading_from_source_time": expected_reading,
        "source_time_aligned": source_time_aligned,
        "sequence_valid": bool(cf.get("sequence_valid", True)),
        "window_integrity": bool(cf.get("window_integrity", True)),
        "entry_band": _entry_band(reading),
        "brti_value": round(current_value, 8),
        "observed_average": round(observed_average, 8),
        "probability": probability,
        "selected": selected,
        "reasons": list(dict.fromkeys(reasons)),
        "quote_age_seconds": book.get("age_seconds"),
        "brti_age_seconds": cf.get("age_seconds"),
        "source_ts_ms": cf.get("source_ts_ms"),
        "upstream_received_at_ms": cf.get("upstream_received_at_ms"),
        "brti_received_at": cf.get("received_at"),
        "orderbook_received_at": book.get("received_at"),
        "latest_brti_move": round(latest_move, 8),
        "spike_limit": round(spike_limit, 8),
        "volatility_samples": volatility_samples,
    }


def _capture_fixed_observation(ledger, market, review, config):
    captured = _parse_time(review.get("reviewed_at"))
    close = _parse_time(market.get("close_time"))
    source_ms = _number(review.get("source_ts_ms"))
    if (not captured or not close or captured >= close or source_ms <= 0
            or captured.timestamp() * 1000 - source_ms > 2000
            or source_ms >= close.timestamp() * 1000):
        ledger.setdefault("observation_rejections", {})
        reason = "late_or_unverifiable_source_arrival"
        ledger["observation_rejections"][reason] = ledger["observation_rejections"].get(reason, 0) + 1
        return None
    reading = _integer(review.get("reading"))
    if reading not in config["observation_readings"]:
        return None
    if not (
        review.get("source_time_aligned")
        and review.get("sequence_valid")
        and review.get("window_integrity")
        and _number(review.get("target")) > 0
        and _number(review.get("brti_value")) > 0
        and _number(review.get("observed_average")) > 0
    ):
        return None
    probability = dict(review.get("probability") or {})
    if probability.get("p_yes") is None:
        return None
    record_id = f"{market.get('ticker')}:reading:{reading}"
    if any(str(row.get("id") or "") == record_id for row in ledger.get("observations") or []):
        return None
    row = {
        "id": record_id,
        "version": VERSION,
        "mode": MODE,
        "affects_execution": False,
        "status": "open",
        "ticker": market.get("ticker"),
        "close_time": market.get("close_time"),
        "target": market.get("target"),
        "market_kind": market.get("market_kind") or "above",
        "captured_at": review.get("reviewed_at"),
        "source_ts_ms": review.get("source_ts_ms"),
        "reading": reading,
        "expected_reading_from_source_time": review.get(
            "expected_reading_from_source_time"
        ),
        "source_time_aligned": True,
        "sequence_valid": bool(review.get("sequence_valid")),
        "window_integrity": bool(review.get("window_integrity")),
        "valid_for_evaluation": True,
        "brti_value": review.get("brti_value"),
        "observed_average": review.get("observed_average"),
        "p_yes": probability.get("p_yes"),
        "p_low": probability.get("p_low"),
        "p_high": probability.get("p_high"),
        "probability_method": probability.get("method"),
        "probability": probability,
        "settlement_checks": 0,
    }
    ledger.setdefault("observations", []).append(row)
    return row


def _capture_policy_arms(ledger, market, review, config):
    if review.get("reasons"):
        return []
    selected = review.get("selected") or {}
    band = review.get("entry_band")
    if band == "outside":
        return []
    existing = {str(row.get("id") or "") for row in ledger.get("records") or []}
    market_has_primary = any(
        row.get("ticker") == market.get("ticker")
        and row.get("valid_for_evaluation", True)
        and row.get("market_primary")
        for row in ledger.get("records") or []
    )
    added = []
    for threshold in config["edge_thresholds_cents"]:
        record_id = f"{market.get('ticker')}:{band}:{threshold:g}c"
        if record_id in existing or _number(selected.get("expected_edge_cents")) < threshold:
            continue
        execution = selected.get("execution") or {}
        fee = selected.get("fee") or {}
        contracts = config["virtual_contracts"]
        total_cost = (
            contracts * _number(execution.get("vwap_cents")) / 100.0
            + _number(fee.get("fee_dollars"))
        )
        row = {
            "id": record_id,
            "version": VERSION,
            "strategy": "btc_15m_settlement_lag_v1",
            "mode": MODE,
            "affects_execution": False,
            "automatic_promotion": False,
            "status": "open",
            "ticker": market.get("ticker"),
            "asset": "BTC",
            "close_time": market.get("close_time"),
            "target": market.get("target"),
            "market_kind": market.get("market_kind") or "above",
            "side": selected.get("side"),
            "entry_band": band,
            "edge_threshold_cents": threshold,
            "captured_at": review.get("reviewed_at"),
            "source_ts_ms": review.get("source_ts_ms"),
            "reading": review.get("reading"),
            "expected_reading_from_source_time": review.get(
                "expected_reading_from_source_time"
            ),
            "source_time_aligned": bool(review.get("source_time_aligned")),
            "valid_for_evaluation": True,
            "market_primary": not market_has_primary,
            "evaluation_status": "valid_time_aligned",
            "remaining_readings": (review.get("probability") or {}).get("remaining_readings"),
            "brti_value": review.get("brti_value"),
            "observed_average": review.get("observed_average"),
            "required_remaining_average": (
                review.get("probability") or {}
            ).get("required_remaining_average"),
            "probability": review.get("probability"),
            "selected_probability": selected.get("selected_probability"),
            "selected_probability_low": selected.get("selected_probability_low"),
            "selected_probability_high": selected.get("selected_probability_high"),
            "entry_price_cents": execution.get("vwap_cents"),
            "best_ask_cents": execution.get("best_ask_cents"),
            "spread_cents": execution.get("spread_cents"),
            "book_walk_slippage_cents": execution.get("book_walk_slippage_cents"),
            "expected_edge_cents": selected.get("expected_edge_cents"),
            "edge_low_cents": selected.get("edge_low_cents"),
            "contracts": contracts,
            "fee_dollars": fee.get("fee_dollars"),
            "fee_per_contract_cents": selected.get("exact_fee_cents"),
            "fee_schedule": fee,
            "total_cost_dollars": round(total_cost, 6),
            "timing": {
                "brti_source_ts_ms": review.get("source_ts_ms"),
                "brti_upstream_received_at_ms": review.get("upstream_received_at_ms"),
                "brti_received_at": review.get("brti_received_at"),
                "orderbook_received_at": review.get("orderbook_received_at"),
                "brti_age_seconds": review.get("brti_age_seconds"),
                "quote_age_seconds": review.get("quote_age_seconds"),
            },
            "hold_counterfactual": {"status": "open"},
            "exit_counterfactual": {"status": "monitoring"},
            "reconfirmed_fok_counterfactual": {
                "status": "pending",
                "order_type": "market",
                "time_in_force": "fill_or_kill",
                "contracts": contracts,
                "captured_at": review.get("reviewed_at"),
                "maximum_delay_seconds": config[
                    "fok_reconfirmation_max_delay_seconds"
                ],
            },
            "settlement_checks": 0,
        }
        ledger["records"].append(row)
        market_has_primary = True
        existing.add(record_id)
        added.append(row)
    return added


def _update_reconfirmed_fok_counterfactual(
    ledger, market, review, book, config
):
    updated = []
    reviewed_at = _parse_time(review.get("reviewed_at"))
    for record in ledger.get("records") or []:
        counterfactual = record.get("reconfirmed_fok_counterfactual") or {}
        if (
            record.get("status") != "open"
            or record.get("ticker") != market.get("ticker")
            or counterfactual.get("status") != "pending"
        ):
            continue
        captured_at = _parse_time(record.get("captured_at"))
        if not captured_at or not reviewed_at:
            continue
        delay = max(0.0, (reviewed_at - captured_at).total_seconds())
        if _integer(review.get("reading")) <= _integer(record.get("reading")):
            continue
        integrity_ok = bool(
            review.get("source_time_aligned")
            and review.get("sequence_valid")
            and review.get("window_integrity")
            and book.get("fresh")
            and book.get("sequence_valid", True)
        )
        if delay > config["fok_reconfirmation_max_delay_seconds"]:
            counterfactual.update(
                {
                    "status": "expired_unconfirmed",
                    "reviewed_at": review.get("reviewed_at"),
                    "delay_seconds": round(delay, 6),
                    "reason": "reconfirmation_latency_limit",
                }
            )
            record["reconfirmed_fok_counterfactual"] = counterfactual
            updated.append(record)
            continue
        if not integrity_ok:
            continue
        contracts = _integer(record.get("contracts"))
        execution = executable_entry(book, record.get("side"), contracts)
        if not execution.get("full_fill") or execution.get("vwap_cents") is None:
            counterfactual.update(
                {
                    "status": "rejected_no_fill",
                    "reviewed_at": review.get("reviewed_at"),
                    "delay_seconds": round(delay, 6),
                    "reason": "fok_depth_unavailable",
                }
            )
            record["reconfirmed_fok_counterfactual"] = counterfactual
            updated.append(record)
            continue
        fee = _execution_fee(execution, market.get("fee_schedule") or {})
        if not fee.get("exact"):
            continue
        total_cost = (
            contracts * _number(execution.get("vwap_cents")) / 100.0
            + _number(fee.get("fee_dollars"))
        )
        counterfactual.update(
            {
                "status": "filled",
                "filled_at": review.get("reviewed_at"),
                "reading": review.get("reading"),
                "delay_seconds": round(delay, 6),
                "entry_price_cents": execution.get("vwap_cents"),
                "best_ask_cents": execution.get("best_ask_cents"),
                "book_walk_slippage_cents": execution.get(
                    "book_walk_slippage_cents"
                ),
                "spread_cents": execution.get("spread_cents"),
                "entry_slippage_vs_initial_cents": round(
                    _number(execution.get("vwap_cents"))
                    - _number(record.get("entry_price_cents")),
                    6,
                ),
                "fee_dollars": fee.get("fee_dollars"),
                "fee_schedule": fee,
                "total_cost_dollars": round(total_cost, 6),
                "virtual_profit": None,
            }
        )
        record["reconfirmed_fok_counterfactual"] = counterfactual
        updated.append(record)
    return updated


def _update_exit_counterfactual(ledger, market, review, book):
    updated = []
    for record in ledger.get("records") or []:
        if record.get("status") != "open" or record.get("ticker") != market.get("ticker"):
            continue
        exit_row = record.get("exit_counterfactual") or {}
        if exit_row.get("status") == "exited":
            continue
        if _integer(review.get("reading")) <= _integer(record.get("reading")):
            continue
        probability = review.get("probability") or {}
        selected, _low, _high = _selected_probability(probability, record.get("side"))
        execution = executable_exit(book, record.get("side"), record.get("contracts"))
        if not execution.get("full_fill") or execution.get("vwap_cents") is None:
            continue
        fee = _execution_fee(execution, market.get("fee_schedule") or {})
        if not fee.get("exact"):
            continue
        net_exit_cents = execution["vwap_cents"] - (
            _number(fee.get("fee_per_contract_dollars")) * 100.0
        )
        current_entry = executable_entry(book, record.get("side"), record.get("contracts"))
        current_entry_fee = _execution_fee(
            current_entry,
            market.get("fee_schedule") or {},
        )
        current_entry_edge = (
            selected
            - _number(current_entry.get("vwap_cents"))
            - _number(current_entry_fee.get("fee_per_contract_dollars")) * 100.0
        )
        catchup = net_exit_cents >= selected - 1.0
        edge_disappeared = current_entry.get("full_fill") and current_entry_edge <= 0
        if not (catchup or edge_disappeared):
            continue
        contracts = _integer(record.get("contracts"))
        proceeds = contracts * execution["vwap_cents"] / 100.0 - _number(fee.get("fee_dollars"))
        profit = proceeds - _number(record.get("total_cost_dollars"))
        record["exit_counterfactual"] = {
            "status": "exited",
            "reason": "market_caught_fair_value" if catchup else "model_edge_disappeared",
            "exited_at": review.get("reviewed_at"),
            "reading": review.get("reading"),
            "selected_probability": round(selected, 4),
            "exit_price_cents": execution.get("vwap_cents"),
            "exit_fee_dollars": fee.get("fee_dollars"),
            "net_exit_cents": round(net_exit_cents, 4),
            "virtual_profit": round(profit, 6),
        }
        updated.append(record)
    return updated


def process_tick(ledger, market, cf, history, book, settings=None):
    config = configuration(settings)
    review = _evaluate_opportunity(ledger, market, cf, history, book, config)
    window_key = str(_integer(cf.get("window_close_ts_ms")))
    window = ledger.setdefault("windows", {}).setdefault(window_key, {
        "window_close_ts_ms": cf.get("window_close_ts_ms"),
        "close_time": market.get("close_time"),
        "ticker": market.get("ticker"),
        "ticks": {},
        "calibration_finalized": False,
    })
    window.setdefault("ticks", {})[str(review["reading"])] = {
        "value": review.get("brti_value"),
        "observed_average": review.get("observed_average"),
        "tick_sigma": (review.get("probability") or {}).get("tick_sigma"),
        "source_ts_ms": review.get("source_ts_ms"),
        "source_time_aligned": bool(review.get("source_time_aligned")),
        "expected_reading_from_source_time": review.get(
            "expected_reading_from_source_time"
        ),
    }
    if review["reading"] >= 60:
        _finalize_calibration_window(ledger, window)
    observation = _capture_fixed_observation(ledger, market, review, config)
    fok_updates = _update_reconfirmed_fok_counterfactual(
        ledger, market, review, book, config
    )
    exit_updates = _update_exit_counterfactual(ledger, market, review, book)
    added = _capture_policy_arms(ledger, market, review, config)
    threshold_reasons = Counter(review.get("reasons") or [])
    selected = review.get("selected") or {}
    for threshold in config["edge_thresholds_cents"]:
        if selected and _number(selected.get("expected_edge_cents")) < threshold:
            threshold_reasons[f"edge_below_{threshold:g}c"] += 1
    review["threshold_reasons"] = dict(threshold_reasons)
    review["captured"] = len(added)
    review["observation_captured"] = bool(observation)
    review["exit_updates"] = len(exit_updates)
    review["fok_reconfirmation_updates"] = len(fok_updates)
    ledger["last_tick_review"] = review
    ledger["updated_at"] = _iso()
    day = _observation_date(review.get("close_time") or review.get("reviewed_at"))
    if day:
        ledger["observation_dates"] = sorted(
            set(ledger.get("observation_dates") or []) | {day}
        )
    # Window data is research-only and bounded independently from records.
    if len(ledger["windows"]) > 120:
        ordered = sorted(ledger["windows"], key=lambda value: _integer(value))
        for key in ordered[:-120]:
            ledger["windows"].pop(key, None)
    if len(ledger["records"]) > config["max_records"]:
        open_rows = [row for row in ledger["records"] if row.get("status") == "open"]
        settled_rows = [row for row in ledger["records"] if row.get("status") != "open"]
        keep = max(0, config["max_records"] - len(open_rows))
        ledger["records"] = (settled_rows[-keep:] if keep else []) + open_rows
    if len(ledger.get("observations") or []) > config["max_records"]:
        open_observations = [
            row for row in ledger["observations"] if row.get("status") == "open"
        ]
        settled_observations = [
            row for row in ledger["observations"] if row.get("status") != "open"
        ]
        keep = max(0, config["max_records"] - len(open_observations))
        ledger["observations"] = (
            settled_observations[-keep:] if keep else []
        ) + open_observations
    return review, added, exit_updates


def settle_records(ledger, fetch_market, settings=None, now=None):
    config = configuration(settings)
    current = _parse_time(now) or datetime.now(timezone.utc)
    settled = []
    payload_cache = {}
    for record in ledger.get("records") or []:
        if record.get("status") != "open":
            continue
        close = _parse_time(record.get("close_time"))
        if not close or (current - close).total_seconds() < config["settlement_grace_minutes"] * 60.0:
            continue
        next_check = _parse_time(record.get("next_settlement_check_at"))
        if next_check and current < next_check:
            continue
        ticker = str(record.get("ticker") or "")
        if ticker not in payload_cache:
            try:
                payload_cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                payload_cache[ticker] = {"status": "error", "error": type(exc).__name__}
        record["settlement_checks"] = _integer(record.get("settlement_checks")) + 1
        finalized, result, settled_at = _market_result(payload_cache[ticker])
        if not finalized:
            record["next_settlement_check_at"] = _iso(
                current.timestamp() + config["settlement_retry_seconds"]
            )
            continue
        won = result == str(record.get("side") or "").lower()
        payout = _integer(record.get("contracts")) if won else 0.0
        hold_profit = payout - _number(record.get("total_cost_dollars"))
        exit_row = record.get("exit_counterfactual") or {}
        if exit_row.get("status") != "exited":
            exit_row = {
                **exit_row,
                "status": "held_to_settlement",
                "virtual_profit": round(hold_profit, 6),
            }
        predicted = _number(record.get("selected_probability")) / 100.0
        fok = dict(record.get("reconfirmed_fok_counterfactual") or {})
        if fok.get("status") == "filled":
            fok_payout = _integer(fok.get("contracts")) if won else 0.0
            fok.update(
                {
                    "status": "settled",
                    "won": won,
                    "result": result,
                    "virtual_profit": round(
                        fok_payout - _number(fok.get("total_cost_dollars")), 6
                    ),
                }
            )
        elif fok.get("status") == "pending":
            fok.update(
                {
                    "status": "unobserved_before_settlement",
                    "result": result,
                    "reason": "no_subsequent_reconfirmation_tick",
                }
            )
        record.update({
            "status": "settled",
            "result": result,
            "won": won,
            "settled_at": settled_at or _iso(current.timestamp()),
            "hold_counterfactual": {
                "status": "settled",
                "virtual_profit": round(hold_profit, 6),
            },
            "exit_counterfactual": exit_row,
            "reconfirmed_fok_counterfactual": fok,
            "brier_score": round((predicted - (1.0 if won else 0.0)) ** 2, 8),
        })
        settled.append(record)
    observation_settled = []
    for observation in ledger.get("observations") or []:
        if observation.get("status") != "open":
            continue
        close = _parse_time(observation.get("close_time"))
        if not close or (current - close).total_seconds() < config["settlement_grace_minutes"] * 60.0:
            continue
        next_check = _parse_time(observation.get("next_settlement_check_at"))
        if next_check and current < next_check:
            continue
        ticker = str(observation.get("ticker") or "")
        if ticker not in payload_cache:
            try:
                payload_cache[ticker] = fetch_market(ticker) or {}
            except Exception as exc:
                payload_cache[ticker] = {
                    "status": "error",
                    "error": type(exc).__name__,
                }
        observation["settlement_checks"] = _integer(
            observation.get("settlement_checks")
        ) + 1
        finalized, result, settled_at = _market_result(payload_cache[ticker])
        if not finalized:
            observation["next_settlement_check_at"] = _iso(
                current.timestamp() + config["settlement_retry_seconds"]
            )
            continue
        outcome_yes = 1.0 if result == "yes" else 0.0
        predicted = min(1.0 - 1e-9, max(1e-9, _number(observation.get("p_yes")) / 100.0))
        observation.update({
            "status": "settled",
            "result": result,
            "settled_at": settled_at or _iso(current.timestamp()),
            "brier_score": round((predicted - outcome_yes) ** 2, 8),
            "log_loss": round(
                -(outcome_yes * math.log(predicted) + (1.0 - outcome_yes) * math.log(1.0 - predicted)),
                8,
            ),
        })
        observation_settled.append(observation)
    if settled or observation_settled:
        ledger["updated_at"] = _iso()
    return settled


def _profit_summary(rows, getter):
    profit = sum(_number(getter(row)) for row in rows)
    cost = sum(_number(row.get("total_cost_dollars")) for row in rows)
    return {
        "settled": len(rows),
        "profit": round(profit, 6),
        "roi": round(100.0 * profit / cost, 2) if cost else 0.0,
    }


def _day_clustered_lower_bound(rows, getter):
    values = [_number(getter(row)) for row in rows]
    groups = {}
    for row, value in zip(rows, values):
        day = _observation_date(row.get("close_time") or row.get("captured_at"))
        if day:
            groups.setdefault(day, []).append(value)
    if not values or len(groups) < 2:
        return {
            "point_estimate": round(sum(values) / len(values), 6) if values else None,
            "standard_error": None,
            "one_sided_90_lower": None,
            "day_clusters": len(groups),
        }
    mean = sum(values) / len(values)
    scores = [sum(value - mean for value in group) for group in groups.values()]
    variance = (len(groups) / (len(groups) - 1.0)) * sum(
        score * score for score in scores
    ) / (len(values) ** 2)
    standard_error = math.sqrt(max(0.0, variance))
    return {
        "point_estimate": round(mean, 6),
        "standard_error": round(standard_error, 6),
        "one_sided_90_lower": round(
            mean - ONE_SIDED_90_Z * standard_error, 6
        ),
        "day_clusters": len(groups),
    }


def summarize(ledger, settings=None):
    config = configuration(settings)
    records = list(ledger.get("records") or [])
    valid_records = [row for row in records if row.get("valid_for_evaluation", True)]
    invalid_records = [row for row in records if not row.get("valid_for_evaluation", True)]
    policy_settled = [row for row in valid_records if row.get("status") == "settled"]
    policy_open = [row for row in valid_records if row.get("status") == "open"]
    primary_records = [row for row in valid_records if row.get("market_primary")]
    settled = [row for row in primary_records if row.get("status") == "settled"]
    open_rows = [row for row in primary_records if row.get("status") == "open"]
    hold_profit = sum(_number((row.get("hold_counterfactual") or {}).get("virtual_profit")) for row in settled)
    exit_profit = sum(_number((row.get("exit_counterfactual") or {}).get("virtual_profit")) for row in settled)
    cost = sum(_number(row.get("total_cost_dollars")) for row in settled)
    unique_markets = {str(row.get("ticker") or "") for row in settled}
    trade_days = {
        _observation_date(row.get("captured_at") or row.get("close_time"))
        for row in settled
        if _observation_date(row.get("captured_at") or row.get("close_time"))
    }
    observation_days = set(ledger.get("observation_dates") or [])
    ordered_settled = sorted(
        settled, key=lambda row: str(row.get("captured_at") or "")
    )
    split = len(ordered_settled) // 2
    hold_getter = lambda row: (row.get("hold_counterfactual") or {}).get(
        "virtual_profit"
    )
    hold_halves = {
        "first": _profit_summary(ordered_settled[:split], hold_getter),
        "second": _profit_summary(ordered_settled[split:], hold_getter),
    }
    hold_inference = _day_clustered_lower_bound(ordered_settled, hold_getter)
    fok_settled = [
        row
        for row in ordered_settled
        if (row.get("reconfirmed_fok_counterfactual") or {}).get("status")
        == "settled"
    ]
    fok_getter = lambda row: (
        row.get("reconfirmed_fok_counterfactual") or {}
    ).get("virtual_profit")
    fok_profit = sum(_number(fok_getter(row)) for row in fok_settled)
    fok_cost = sum(
        _number(
            (row.get("reconfirmed_fok_counterfactual") or {}).get(
                "total_cost_dollars"
            )
        )
        for row in fok_settled
    )
    fok_inference = _day_clustered_lower_bound(fok_settled, fok_getter)
    integrity_ok = bool(ordered_settled) and all(
        row.get("source_time_aligned")
        and (row.get("fee_schedule") or {}).get("exact")
        and _number(row.get("spread_cents"), 999.0)
        <= config["maximum_spread_cents"]
        and _number((row.get("timing") or {}).get("quote_age_seconds"), 999.0)
        <= config["maximum_quote_age_seconds"]
        for row in ordered_settled
    )
    both_halves_profitable = bool(
        hold_halves["first"]["settled"]
        and hold_halves["second"]["settled"]
        and hold_halves["first"]["profit"] > 0
        and hold_halves["second"]["profit"] > 0
    )
    breakdown = []
    for threshold in config["edge_thresholds_cents"]:
        for band in ("25-34", "35-44", "45-52"):
            rows = [
                row for row in policy_settled
                if _number(row.get("edge_threshold_cents")) == threshold
                and row.get("entry_band") == band
            ]
            row_cost = sum(_number(row.get("total_cost_dollars")) for row in rows)
            row_profit = sum(
                _number((row.get("hold_counterfactual") or {}).get("virtual_profit"))
                for row in rows
            )
            breakdown.append({
                "threshold_cents": threshold,
                "entry_band": band,
                "settled": len(rows),
                "wins": sum(1 for row in rows if row.get("won")),
                "losses": sum(1 for row in rows if not row.get("won")),
                "profit": round(row_profit, 4),
                "roi": round(row_profit / row_cost * 100.0, 2) if row_cost else 0.0,
            })
    calibration_count = sum(len(values or []) for values in (ledger.get("calibration_samples") or {}).values())
    review = dict(ledger.get("last_tick_review") or {})
    health = dict(ledger.get("health") or {})
    observations = [
        row for row in (ledger.get("observations") or [])
        if row.get("valid_for_evaluation", True)
        and _parse_time(row.get("captured_at")) and _parse_time(row.get("close_time"))
        and _parse_time(row.get("captured_at")) < _parse_time(row.get("close_time"))
        and 0 <= _parse_time(row.get("captured_at")).timestamp() * 1000 - _number(row.get("source_ts_ms")) <= 2000
    ]
    settled_observations = [
        row for row in observations if row.get("status") == "settled"
    ]
    observation_breakdown = []
    for reading in config["observation_readings"]:
        rows = [
            row for row in settled_observations
            if _integer(row.get("reading")) == reading
        ]
        observation_breakdown.append({
            "reading": reading,
            "tracked": sum(_integer(row.get("reading")) == reading for row in observations),
            "settled": len(rows),
            "average_brier": round(
                sum(_number(row.get("brier_score")) for row in rows) / len(rows), 6
            ) if rows else None,
            "average_log_loss": round(
                sum(_number(row.get("log_loss")) for row in rows) / len(rows), 6
            ) if rows else None,
            "mean_predicted_yes": round(
                sum(_number(row.get("p_yes")) for row in rows) / len(rows), 2
            ) if rows else None,
            "observed_yes_rate": round(
                100.0 * sum(row.get("result") == "yes" for row in rows) / len(rows), 2
            ) if rows else None,
        })
    summary = {
        "version": VERSION,
        "integrity_revision": INTEGRITY_REVISION,
        "mode": MODE if config["enabled"] else "disabled",
        "affects_execution": False,
        "automatic_promotion": False,
        "updated_at": ledger.get("updated_at"),
        "health": health,
        "configuration": config,
        "tracked": len(primary_records),
        "policy_arm_tracked": len(valid_records),
        "diagnostic_invalid_records": len(invalid_records),
        "diagnostic_invalid_settled": sum(
            row.get("status") == "settled" for row in invalid_records
        ),
        "diagnostic_invalid_hold_profit": round(sum(
            _number((row.get("hold_counterfactual") or {}).get("virtual_profit"))
            for row in invalid_records if row.get("status") == "settled"
        ), 4),
        "open": len(open_rows),
        "settled": len(settled),
        "wins": sum(1 for row in settled if row.get("won")),
        "losses": sum(1 for row in settled if not row.get("won")),
        "independent_markets": len(unique_markets),
        "observation_days": len(observation_days),
        "settled_trade_days": len(trade_days),
        "hold_profit": round(hold_profit, 4),
        "hold_roi": round(hold_profit / cost * 100.0, 2) if cost else 0.0,
        "hold_chronological_halves": hold_halves,
        "hold_day_cluster_inference": hold_inference,
        "reconfirmed_fok": {
            "settled": len(fok_settled),
            "profit": round(fok_profit, 4),
            "roi": round(100.0 * fok_profit / fok_cost, 2) if fok_cost else 0.0,
            "day_cluster_inference": fok_inference,
            "average_delay_seconds": round(
                sum(
                    _number(
                        (row.get("reconfirmed_fok_counterfactual") or {}).get(
                            "delay_seconds"
                        )
                    )
                    for row in fok_settled
                )
                / len(fok_settled),
                4,
            )
            if fok_settled
            else None,
            "average_entry_slippage_cents": round(
                sum(
                    _number(
                        (row.get("reconfirmed_fok_counterfactual") or {}).get(
                            "entry_slippage_vs_initial_cents"
                        )
                    )
                    for row in fok_settled
                )
                / len(fok_settled),
                4,
            )
            if fok_settled
            else None,
            "status_counts": dict(
                Counter(
                    str(
                        (row.get("reconfirmed_fok_counterfactual") or {}).get(
                            "status"
                        )
                        or "legacy_unavailable"
                    )
                    for row in primary_records
                )
            ),
        },
        "exit_policy_profit": round(exit_profit, 4),
        "exit_policy_roi": round(exit_profit / cost * 100.0, 2) if cost else 0.0,
        "average_brier": round(
            sum(_number(row.get("brier_score")) for row in settled) / len(settled), 6
        ) if settled else None,
        "policy_arm_open": len(policy_open),
        "policy_arm_settled": len(policy_settled),
        "policy_arm_wins": sum(1 for row in policy_settled if row.get("won")),
        "policy_arm_losses": sum(1 for row in policy_settled if not row.get("won")),
        "policy_arm_hold_profit": round(sum(
            _number((row.get("hold_counterfactual") or {}).get("virtual_profit"))
            for row in policy_settled
        ), 4),
        "observations": {
            "tracked": len(observations),
            "open": sum(row.get("status") == "open" for row in observations),
            "settled": len(settled_observations),
            "average_brier": round(
                sum(_number(row.get("brier_score")) for row in settled_observations)
                / len(settled_observations), 6
            ) if settled_observations else None,
            "average_log_loss": round(
                sum(_number(row.get("log_loss")) for row in settled_observations)
                / len(settled_observations), 6
            ) if settled_observations else None,
            "by_reading": observation_breakdown,
        },
        "calibration_residual_samples": calibration_count,
        "current_window": review,
        "policy_breakdown": breakdown,
        "recent_records": list(reversed(records[-50:])),
        "recent_observations": list(reversed(observations[-50:])),
        "evaluation": {
            "minimum_independent_markets": config["minimum_independent_markets"],
            "minimum_observation_days": config["minimum_observation_days"],
            "gates": {
                "minimum_independent_markets": len(unique_markets)
                >= config["minimum_independent_markets"],
                "minimum_observation_days": len(observation_days)
                >= config["minimum_observation_days"],
                "positive_after_fee_hold_profit": hold_profit > 0,
                "both_chronological_halves_profitable": both_halves_profitable,
                "positive_day_clustered_hold_lower_bound": hold_inference[
                    "one_sided_90_lower"
                ]
                is not None
                and hold_inference["one_sided_90_lower"] > 0,
                "capture_integrity_verified": integrity_ok,
                "minimum_reconfirmed_fok_markets": len(fok_settled)
                >= config["minimum_independent_markets"],
                "positive_reconfirmed_fok_profit": fok_profit > 0,
                "positive_day_clustered_fok_lower_bound": fok_inference[
                    "one_sided_90_lower"
                ]
                is not None
                and fok_inference["one_sided_90_lower"] > 0,
            },
            "ready_for_manual_review": False,
            "automatic_promotion": False,
        },
    }
    summary["evaluation"]["ready_for_manual_review"] = all(
        summary["evaluation"]["gates"].values()
    )
    return summary


class SettlementLagShadowWorker:
    """Background read-only evaluator driven by fresh BRTI stream updates."""

    def __init__(self, ledger_path, *, fetch_market, logger=None, event_logger=None, research_sink=None):
        self.ledger_path = Path(ledger_path)
        self.fetch_market = fetch_market
        self.logger = logger
        self.event_logger = event_logger
        self.research_sink = research_sink
        self._lock = threading.RLock()
        self._stream = None
        self._markets = {}
        self._settings = {}
        self._thread = None
        self._stop = threading.Event()
        self._last_source_ts_ms = 0
        self._last_settlement_check = 0.0
        self._last_health_persist = 0.0

    def configure(self, *, stream, markets, settings=None):
        with self._lock:
            self._stream = stream
            self._settings = dict(settings or {})
            now = time.time()
            merged = dict(self._markets)
            for market in markets or []:
                ticker = str(market.get("ticker") or "")
                if ticker:
                    merged[ticker] = dict(market)
            self._markets = {
                ticker: market
                for ticker, market in merged.items()
                if not _parse_time(market.get("close_time"))
                or (_parse_time(market.get("close_time")).timestamp() >= now - 1800)
            }

    def start(self):
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="btc-settlement-lag-shadow",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def status(self):
        with self._lock:
            return {
                "thread_alive": bool(self._thread and self._thread.is_alive()),
                "registered_markets": len(self._markets),
                "last_source_ts_ms": self._last_source_ts_ms,
                "affects_execution": False,
            }

    def _matching_market(self, window_close_ts_ms):
        with self._lock:
            markets = list(self._markets.values())
        for market in markets:
            close = _parse_time(market.get("close_time"))
            if close and abs(close.timestamp() * 1000.0 - _number(window_close_ts_ms)) <= 1500:
                return market
        return None

    def _persist(self, ledger):
        ledger["summary"] = summarize(ledger, self._settings)
        _write_json(self.ledger_path, ledger)

    def _emit(self, event_type, row):
        if self.event_logger:
            try:
                self.event_logger({"type": event_type, "shadow": row})
            except Exception:
                pass

    def _log(self, text):
        if self.logger:
            try:
                self.logger(text)
            except Exception:
                pass

    @staticmethod
    def _prepare_cf_update(row, maximum_age_seconds):
        prepared = dict(row or {})
        received_unix = _number(prepared.get("received_unix"))
        age = max(0.0, time.time() - received_unix) if received_unix else float("inf")
        prepared["age_seconds"] = round(age, 3) if math.isfinite(age) else None
        prepared["fresh"] = bool(
            age <= maximum_age_seconds
            and prepared.get("sequence_valid", True)
            and prepared.get("window_integrity", True)
        )
        return prepared

    @staticmethod
    def _captured_book(cf, ticker, maximum_age_seconds):
        book = dict(((cf.get("market_snapshots") or {}).get(str(ticker or ""))) or {})
        if not book:
            return {}
        book_received_unix = _number(book.get("book_received_unix"))
        cf_received_unix = _number(cf.get("received_unix"))
        age = (
            max(0.0, cf_received_unix - book_received_unix)
            if book_received_unix and cf_received_unix
            else float("inf")
        )
        book["age_seconds"] = round(age, 3) if math.isfinite(age) else None
        book["fresh"] = bool(
            age <= maximum_age_seconds and book.get("sequence_valid", False)
            and book.get("book_snapshot_valid", False)
        )
        return book

    def _run(self):
        ledger = normalize_ledger(_read_json(self.ledger_path, {}), self._settings)
        while not self._stop.is_set():
            try:
                with self._lock:
                    stream = self._stream
                    settings = dict(self._settings)
                config = configuration(settings)
                if not config["enabled"] or stream is None:
                    ledger["health"] = {"status": "waiting_for_stream", **self.status()}
                    if time.time() - self._last_health_persist >= 30.0:
                        self._persist(ledger)
                        self._last_health_persist = time.time()
                    self._stop.wait(1.0)
                    continue
                history = stream.cfbenchmark_history(
                    config["index_id"],
                    seconds=config["volatility_lookback_seconds"],
                )
                updates = sorted(
                    (
                        row for row in history
                        if _integer(row.get("source_ts_ms")) > self._last_source_ts_ms
                    ),
                    key=lambda row: _integer(row.get("source_ts_ms")),
                )
                processed_final_tick = False
                latest_cf = None
                for raw_cf in updates:
                    cf = self._prepare_cf_update(
                        raw_cf,
                        config["maximum_quote_age_seconds"],
                    )
                    source_ts_ms = _integer(cf.get("source_ts_ms"))
                    self._last_source_ts_ms = source_ts_ms
                    latest_cf = cf
                    market = self._matching_market(cf.get("window_close_ts_ms"))
                    if _integer(cf.get("window_size")) <= 0 or not market:
                        continue
                    processed_final_tick = True
                    book = self._captured_book(
                        cf,
                        market.get("ticker"),
                        config["maximum_quote_age_seconds"],
                    )
                    history_so_far = [
                        row for row in history
                        if _integer(row.get("source_ts_ms")) <= source_ts_ms
                    ]
                    review, added, exits = process_tick(
                        ledger, market, cf, history_so_far, book, settings
                    )
                    if self.research_sink:
                        self.research_sink(market, review, cf, book, history_so_far)
                    for row in added:
                        self._emit("crypto_settlement_lag_shadow_captured", row)
                        self._log(
                            "CRYPTO SETTLEMENT LAG SHADOW CAPTURED: "
                            f"{str(row.get('side') or '').upper()} {row.get('ticker')} "
                            f"k={row.get('reading')} entry={row.get('entry_price_cents')}c "
                            f"edge={row.get('expected_edge_cents')}c "
                            f"low={row.get('edge_low_cents')}c arm={row.get('edge_threshold_cents')}c"
                        )
                    for row in exits:
                        self._emit("crypto_settlement_lag_shadow_exited", row)
                    ledger["health"] = {
                        "status": "collecting_final_minute",
                        "index_id": config["index_id"],
                        "feed_fresh": bool(cf.get("fresh")),
                        "window_integrity": bool(cf.get("window_integrity", True)),
                        **self.status(),
                    }
                if processed_final_tick:
                    self._persist(ledger)
                    self._last_health_persist = time.time()
                elif latest_cf:
                    ledger["health"] = {
                        "status": "waiting_for_final_minute",
                        "index_id": config["index_id"],
                        "feed_fresh": bool(latest_cf.get("fresh")),
                        **self.status(),
                    }
                    if time.time() - self._last_health_persist >= 30.0:
                        self._persist(ledger)
                        self._last_health_persist = time.time()
                if time.time() - self._last_settlement_check >= 5.0:
                    self._last_settlement_check = time.time()
                    settled = settle_records(ledger, self.fetch_market, settings)
                    if settled:
                        for row in settled:
                            self._emit("crypto_settlement_lag_shadow_settled", row)
                            self._log(
                                "CRYPTO SETTLEMENT LAG SHADOW SETTLED: "
                                f"{'WIN' if row.get('won') else 'LOSS'} {row.get('ticker')} "
                                f"hold=${_number((row.get('hold_counterfactual') or {}).get('virtual_profit')):.2f} "
                                f"exit=${_number((row.get('exit_counterfactual') or {}).get('virtual_profit')):.2f}"
                            )
                        self._persist(ledger)
                        self._last_health_persist = time.time()
            except Exception as exc:
                ledger["health"] = {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    **self.status(),
                }
                try:
                    self._persist(ledger)
                except Exception:
                    pass
                self._stop.wait(1.0)
                continue
            self._stop.wait(0.10)
