"""Settlement-aware probability support and exact Kalshi cost calculations.

The functions in this module are intentionally pure so pricing, fee rounding,
and conservative-edge decisions can be replayed in tests and offline audits.
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


TAKER_QUADRATIC_RATE = 0.07
MAKER_QUADRATIC_RATE = 0.0175
FEE_QUANTUM_DOLLARS = Decimal("0.0001")
TRADE_FEE_QUANTUM_DOLLARS = Decimal("0.000001")
FEE_MODEL_VERSION = "kalshi-trade-and-balance-rounding-2026-09-v1"


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def clamp(value, low=0.0, high=1.0):
    return max(float(low), min(float(high), float(value)))


def norm_cdf(value):
    return 0.5 * (1.0 + math.erf(float(value) / math.sqrt(2.0)))


def ceil_fee(value, quantum=FEE_QUANTUM_DOLLARS):
    """Round an exchange trade-fee amount up to Kalshi's centicent quantum."""
    amount = max(Decimal("0"), Decimal(str(value)))
    if amount <= 0:
        return 0.0
    units = (amount / quantum).to_integral_value(rounding=ROUND_CEILING)
    return float(units * quantum)


def kalshi_fill_fee(revenue, model_fee, *, balance_precision, accumulator=0):
    """Pure per-fill cash/fee accounting; caller supplies verified member precision.

    No account type is inferred. The returned accumulator belongs to this order
    only, including across its taker and maker fills.
    """
    precision = Decimal(str(balance_precision))
    if precision not in {Decimal("0.01"), Decimal("0.0001")}:
        raise ValueError("unknown_balance_precision")
    revenue = Decimal(str(revenue))
    raw = max(Decimal("0"), Decimal(str(model_fee)))
    trade = (raw / TRADE_FEE_QUANTUM_DOLLARS).to_integral_value(rounding=ROUND_CEILING) * TRADE_FEE_QUANTUM_DOLLARS
    change = revenue - trade
    aligned = (change / precision).to_integral_value(rounding=ROUND_FLOOR) * precision
    rounding = change - aligned
    accumulated = Decimal(str(accumulator)) + rounding
    rebate_units = min(
        (accumulated / precision).to_integral_value(rounding=ROUND_FLOOR),
        ((trade + rounding) / precision).to_integral_value(rounding=ROUND_FLOOR),
    )
    rebate = max(Decimal("0"), rebate_units * precision)
    return {key: float(value) for key, value in {
        "trade_fee_dollars": trade, "rounding_fee_dollars": rounding,
        "rebate_dollars": rebate, "net_fee_dollars": trade + rounding - rebate,
        "balance_change_dollars": aligned + rebate,
        "accumulator_dollars": accumulated - rebate,
    }.items()}


def normalized_fee_schedule(schedule=None, fallback_rate=TAKER_QUADRATIC_RATE):
    schedule = schedule or {}
    fee_type = str(schedule.get("fee_type") or "quadratic").strip().lower()
    multiplier = max(0.0, number(schedule.get("fee_multiplier"), 1.0))
    if multiplier == 0 and not schedule.get("authoritative"):
        multiplier = max(0.0, number(fallback_rate, TAKER_QUADRATIC_RATE) / TAKER_QUADRATIC_RATE)
    supported = fee_type in {"quadratic", "quadratic_with_maker_fees"}
    return {
        "fee_type": fee_type,
        "fee_multiplier": multiplier,
        "authoritative": bool(schedule.get("authoritative")),
        "supported": supported,
        "source": str(schedule.get("source") or "configured_fallback"),
        "series_ticker": str(schedule.get("series_ticker") or ""),
        "fetched_at": schedule.get("fetched_at"),
    }


def kalshi_order_fee(
    price_cents,
    contracts=1.0,
    *,
    schedule=None,
    liquidity_role="taker",
    fallback_rate=TAKER_QUADRATIC_RATE,
):
    """Calculate a rounded Kalshi order fee for supported series fee types.

    Unknown/flat schedules deliberately fall back to the standard quadratic
    taker curve and are marked non-exact. That prevents an unknown schedule
    from being treated as free while keeping the bot operational.
    """
    price = clamp(number(price_cents) / 100.0)
    count = max(0.0, number(contracts))
    normalized = normalized_fee_schedule(schedule, fallback_rate=fallback_rate)
    role = str(liquidity_role or "taker").strip().lower()
    exact = normalized["supported"] and normalized["authoritative"]
    if role == "maker" and normalized["fee_type"] == "quadratic":
        coefficient = 0.0
    elif normalized["fee_type"] == "quadratic_with_maker_fees" and role == "maker":
        coefficient = MAKER_QUADRATIC_RATE * normalized["fee_multiplier"]
    else:
        coefficient = TAKER_QUADRATIC_RATE * normalized["fee_multiplier"]
        if not normalized["supported"]:
            coefficient = max(coefficient, number(fallback_rate, TAKER_QUADRATIC_RATE))
            exact = False
    # Multiplying floats before Decimal introduces artificial ceiling increments.
    decimal_price = Decimal(str(price_cents)) / Decimal("100")
    decimal_price = max(Decimal("0"), min(Decimal("1"), decimal_price))
    base_rate = (Decimal("0") if role == "maker" and normalized["fee_type"] == "quadratic"
                 else Decimal("0.0175") if role == "maker" and normalized["fee_type"] == "quadratic_with_maker_fees"
                 else Decimal("0.07"))
    decimal_coefficient = base_rate * Decimal(str(normalized["fee_multiplier"]))
    if not normalized["supported"]:
        decimal_coefficient = max(decimal_coefficient, Decimal(str(fallback_rate)))
    raw_fee = decimal_coefficient * Decimal(str(count)) * decimal_price * (1 - decimal_price)
    rounded_fee = ceil_fee(raw_fee)
    trade_fee = ceil_fee(raw_fee, TRADE_FEE_QUANTUM_DOLLARS)
    principal = Decimal(str(count)) * decimal_price
    conservative_cost = ceil_fee(principal + raw_fee, Decimal("0.01"))
    return {
        **normalized,
        "liquidity_role": role,
        "contracts": round(count, 6),
        "price_cents": round(price * 100.0, 4),
        "coefficient": round(coefficient, 8),
        "raw_fee_dollars": float(raw_fee),
        "trade_fee_dollars": trade_fee,
        "fee_model_version": FEE_MODEL_VERSION,
        "exact_scope": "supported_series_trade_fee_model",
        "net_fee_exact": False,
        "net_fee_status": "member_precision_and_fill_sequence_unverified",
        "conservative_fee_dollars": round(max(0.0, conservative_cost - float(principal)), 6),
        "fee_dollars": round(rounded_fee, 4),
        "fee_per_contract_dollars": round(rounded_fee / count, 6) if count > 0 else 0.0,
        "exact": exact,
    }


def settlement_window_horizons(minutes_to_close, window_seconds=60.0):
    """Return drift and Brownian variance horizons for a final-window average.

    For a one-minute settlement average over [T-1, T], the average observation
    time is T-1/2 and the variance of the averaged Brownian path is T-2/3.
    The live campaign enters with at least several minutes remaining, but the
    bounded fallback also behaves safely inside the settlement window.
    """
    minutes = max(0.0, number(minutes_to_close))
    window_minutes = max(1.0 / 60.0, number(window_seconds, 60.0) / 60.0)
    if minutes >= window_minutes:
        drift_horizon = minutes - window_minutes / 2.0
        variance_horizon = minutes - (2.0 * window_minutes / 3.0)
    else:
        # Part of the official window is already known. The unknown remainder
        # is conservatively represented by its midpoint and Brownian average.
        drift_horizon = minutes / 2.0
        variance_horizon = minutes / 3.0
    return {
        "window_seconds": round(window_minutes * 60.0, 3),
        "drift_minutes": round(max(0.0, drift_horizon), 8),
        "variance_minutes": round(max(1.0 / 180.0, variance_horizon), 8),
    }


def parse_settlement_value(market):
    """Extract the underlying numeric settlement/expiration value if present."""
    market = market or {}
    for key in (
        "expiration_value",
        "settlement_value",
        "settlement_value_dollars",
        "settlement_price",
        "settlement_price_dollars",
    ):
        value = market.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed, key
    return None, None


def data_quality_metrics(underlying=None, kalshi=None, *, maximum_age_seconds=3.0):
    underlying = underlying or {}
    kalshi = kalshi or {}
    maximum_age = max(0.1, number(maximum_age_seconds, 3.0))
    source_count = int(number(underlying.get("settlement_proxy_source_count")))
    coinbase = underlying.get("coinbase") or {}
    kraken = underlying.get("kraken") or {}
    ages = []
    freshness_scores = []
    source_health = {}
    for name, row in (("coinbase", coinbase), ("kraken", kraken)):
        age = row.get("book_age_seconds")
        connected = bool(row.get("connected", bool(row.get("mid"))))
        has_book = bool(number(row.get("mid")) > 0 and age is not None)
        has_trades = bool(number(row.get("trade_count_60s")) > 0)
        if age is None:
            source_health[name] = {
                "connected": connected,
                "book_available": has_book,
                "trades_available": has_trades,
                "book_age_seconds": None,
                "freshness": 0.0,
                "error": row.get("error"),
            }
            continue
        age = max(0.0, number(age))
        ages.append(age)
        freshness = clamp(1.0 - age / maximum_age)
        freshness_scores.append(freshness)
        source_health[name] = {
            "connected": connected,
            "book_available": has_book,
            "trades_available": has_trades,
            "book_age_seconds": round(age, 4),
            "stream_age_seconds": (
                round(max(0.0, number(row.get("stream_age_seconds"))), 4)
                if row.get("stream_age_seconds") is not None
                else None
            ),
            "freshness": round(freshness, 4),
            "event_latency_ms": (
                round(max(0.0, number(row.get("event_latency_ms"))), 3)
                if row.get("event_latency_ms") is not None
                else None
            ),
            "error": row.get("error"),
        }
    kalshi_age = kalshi.get("age_seconds")
    if kalshi_age is None:
        fetched = kalshi.get("orderbook_age_seconds")
        kalshi_age = fetched if fetched is not None else 0.0 if kalshi.get("orderbook_fetched_at") else None
    kalshi_freshness = (
        clamp(1.0 - max(0.0, number(kalshi_age)) / maximum_age)
        if kalshi_age is not None
        else 0.0
    )
    dispersion = max(0.0, number(underlying.get("cross_exchange_dispersion_bps")))
    dispersion_score = clamp(1.0 - dispersion / 30.0)
    completeness = clamp(source_count / 2.0)
    source_freshness = sum(freshness_scores) / len(freshness_scores) if freshness_scores else 0.0
    score = (
        0.35 * completeness
        + 0.30 * source_freshness
        + 0.20 * kalshi_freshness
        + 0.15 * dispersion_score
    )
    reasons = []
    if source_count < 2:
        reasons.append("settlement_proxy_sources_incomplete")
    if source_freshness < 0.5:
        reasons.append("underlying_quotes_stale")
    if kalshi_freshness < 0.5:
        reasons.append("kalshi_quote_stale")
    if dispersion > 20:
        reasons.append("cross_exchange_dispersion_high")
    sequence_valid = bool(kalshi.get("sequence_valid", True))
    if not sequence_valid:
        score *= 0.25
        reasons.append("kalshi_orderbook_sequence_invalid")
    kalshi_has_book = bool(
        kalshi.get("best_yes_entry_price_cents") is not None
        and kalshi.get("best_no_entry_price_cents") is not None
    )
    kalshi_has_trades = bool(number(kalshi.get("trade_count_60s")) > 0)
    return {
        "score": round(clamp(score), 4),
        "source_count": source_count,
        "source_freshness": round(source_freshness, 4),
        "kalshi_freshness": round(kalshi_freshness, 4),
        "cross_exchange_dispersion_bps": round(dispersion, 4),
        "maximum_source_age_seconds": round(max(ages), 4) if ages else None,
        "source_health": {
            **source_health,
            "kalshi": {
                "connected": bool(kalshi.get("fresh", kalshi_has_book)),
                "book_available": kalshi_has_book,
                "trades_available": kalshi_has_trades,
                "sequence_valid": sequence_valid,
                "book_age_seconds": round(max(0.0, number(kalshi_age)), 4)
                if kalshi_age is not None
                else None,
                "freshness": round(kalshi_freshness, 4),
                "event_latency_ms": round(max(0.0, number(kalshi.get("event_latency_ms"))), 3)
                if kalshi.get("event_latency_ms") is not None
                else None,
                "error": kalshi.get("error"),
            },
        },
        "source_presence": {
            "coinbase_book": bool(source_health.get("coinbase", {}).get("book_available")),
            "coinbase_trades": bool(source_health.get("coinbase", {}).get("trades_available")),
            "kraken_book": bool(source_health.get("kraken", {}).get("book_available")),
            "kraken_trades": bool(source_health.get("kraken", {}).get("trades_available")),
            "kalshi_book": kalshi_has_book,
            "kalshi_trades": kalshi_has_trades,
        },
        "reasons": reasons,
    }


def probability_interval(probability_pct, *, model_disagreement_pp=0.0, data_quality=1.0, basis_std_bps=0.0, base_sigma_pp=1.5):
    """Build a conservative, auditable uncertainty interval around p(YES)."""
    probability = clamp(number(probability_pct) / 100.0)
    quality = clamp(data_quality)
    components = {
        "base_model_sigma_pp": max(0.25, number(base_sigma_pp, 1.5)),
        "model_disagreement_sigma_pp": 0.25 * max(0.0, number(model_disagreement_pp)),
        "data_quality_sigma_pp": 4.0 * (1.0 - quality),
        "settlement_basis_sigma_pp": min(3.0, max(0.0, number(basis_std_bps)) / 5.0),
    }
    sigma_pp = sum(components.values())
    width_pp = 1.6448536269514722 * sigma_pp
    return {
        "p_yes": round(probability * 100.0, 3),
        "p_low": round(max(0.01, probability * 100.0 - width_pp), 3),
        "p_high": round(min(99.99, probability * 100.0 + width_pp), 3),
        "probability_sigma_pp": round(sigma_pp, 4),
        "interval_level": 0.90,
        "method": "input_quality_and_model_dispersion_normal_approximation",
        "uncertainty_components": {
            key: round(value, 4) for key, value in components.items()
        },
    }


def conservative_edge_metrics(
    probability_pct,
    side,
    executable_ask_cents,
    *,
    fee_per_contract_dollars=0.0,
    expected_slippage_cents=0.0,
    interval=None,
):
    """Return expected/lower-bound edge and Pr(net edge > 0)."""
    interval = interval or probability_interval(probability_pct)
    side = str(side or "yes").lower()
    p_yes = clamp(number(probability_pct) / 100.0)
    p_side = p_yes if side == "yes" else 1.0 - p_yes
    if side == "yes":
        p_side_low = clamp(number(interval.get("p_low")) / 100.0)
        p_side_high = clamp(number(interval.get("p_high")) / 100.0)
    else:
        p_side_low = 1.0 - clamp(number(interval.get("p_high")) / 100.0)
        p_side_high = 1.0 - clamp(number(interval.get("p_low")) / 100.0)
    ask = max(0.0, number(executable_ask_cents))
    fee_cents = max(0.0, number(fee_per_contract_dollars)) * 100.0
    slippage_cents = max(0.0, number(expected_slippage_cents))
    break_even = clamp((ask + fee_cents + slippage_cents) / 100.0)
    sigma_probability = max(0.0025, number(interval.get("probability_sigma_pp"), 1.5) / 100.0)
    probability_positive = norm_cdf((p_side - break_even) / sigma_probability)
    return {
        "selected_side_probability": round(p_side * 100.0, 3),
        "selected_side_probability_low": round(p_side_low * 100.0, 3),
        "selected_side_probability_high": round(p_side_high * 100.0, 3),
        "executable_ask_cents": round(ask, 4),
        "exact_fee_cents": round(fee_cents, 4),
        "expected_slippage_cents": round(slippage_cents, 4),
        "break_even_probability": round(break_even * 100.0, 3),
        "expected_edge": round(p_side * 100.0 - ask - fee_cents - slippage_cents, 3),
        "edge_low": round(p_side_low * 100.0 - ask - fee_cents - slippage_cents, 3),
        "probability_net_edge_positive": round(probability_positive * 100.0, 2),
    }
