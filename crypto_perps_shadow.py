"""Paper-only Kalshi perpetual-futures research engine.

This module deliberately contains no authenticated order path. It consumes the
public production margin feed, simulates isolated 1x positions, funding, spread,
fees, stops, targets, and time exits, and writes a separate research ledger.
"""

import json
import math
import statistics
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


BASE_URL = "https://external-api.kalshi.com/trade-api/v2/margin"
PORTFOLIO_FILE = Path("crypto_perps_shadow_portfolio.json")
REPORT_FILE = Path("crypto_perps_shadow_report.json")
CACHE_FILE = Path("crypto_perps_shadow_cache.json")
LOG_FILE = Path("crypto_perps_shadow_log.txt")
EVENTS_FILE = Path("crypto_perps_shadow_events.jsonl")
FORECASTS_FILE = Path("crypto_perps_shadow_forecasts.json")

ASSET_BY_TICKER = {
    "KXBTCPERP": "BTC", "KXETHPERP": "ETH", "KXSOLPERP": "SOL",
    "KXXRPPERP": "XRP", "KXDOGEPERP": "DOGE", "KXLINKPERP": "LINK",
    "KXLTCPERP": "LTC", "KXBCHPERP": "BCH", "KXSUIPERP": "SUI",
    "KXNEARPERP": "NEAR", "KXHYPEPERP": "HYPE", "KXKSHIBPERP": "SHIB",
    "KXZECPERP": "ZEC", "KXXLMPERP": "XLM", "KXHBARPERP": "HBAR",
    "KXDOTPERP": "DOT",
}


def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat(timespec="seconds")


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def write_json(path, payload):
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
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
    raise last_error


def log(message):
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")


def event(payload):
    with EVENTS_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"ts": iso_now(), **payload}, sort_keys=True) + "\n")


def http_json(path, params=None, timeout=20):
    url = f"{BASE_URL}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "polymaker-perps-shadow/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def setting_float(settings, key, default):
    return number(settings.get(key, default), default)


def setting_int(settings, key, default):
    try:
        return int(float(settings.get(key, default)))
    except (TypeError, ValueError):
        return int(default)


def setting_bool(settings, key, default=True):
    return str(settings.get(key, str(default).lower())).lower() == "true"


def market_price(market, field):
    value = market.get(field)
    if isinstance(value, dict):
        value = value.get("price")
    return number(value, 0.0)


def plausible_perp_price(value, anchor=None, max_log_move=0.25):
    price = number(value)
    if not math.isfinite(price) or price <= 0 or price >= 1_000_000_000:
        return False
    anchor_price = number(anchor)
    if (
        anchor_price > 0
        and math.isfinite(anchor_price)
        and anchor_price < 1_000_000_000
        and abs(math.log(price) - math.log(anchor_price)) > max_log_move
    ):
        return False
    return True


def executable_market_valid(market):
    """Reject unavailable/sentinel books before any simulated cash mutation.

    Compare quotes with the current mark, not the entry: a genuine large move
    must still be allowed to trigger a losing stop.
    """
    bid, ask = market_price(market, "bid"), market_price(market, "ask")
    mark = market_price(market, "settlement_mark_price")
    return (
        plausible_perp_price(mark)
        and plausible_perp_price(bid, anchor=mark)
        and plausible_perp_price(ask, anchor=mark)
        and bid <= ask
    )


def invalid_settlement_reason(row):
    if not plausible_perp_price(row.get("entry_price")):
        return "invalid_entry_price"
    if not plausible_perp_price(row.get("exit_price")):
        return "invalid_exit_price"
    if not all(math.isfinite(number(row.get(key), float("nan")))
               for key in ("profit", "entry_fee", "exit_fee", "funding_pnl")):
        return "invalid_settlement_economics"
    return ""


def sanitize_forecast_ledger(ledger):
    """Drop API sentinel prices before they can corrupt research analytics."""
    ledger = ledger if isinstance(ledger, dict) else {}
    ledger.setdefault("mode", "research_forecasts_only")
    open_rows = []
    invalid_open = 0
    for row in ledger.get("open") or []:
        if plausible_perp_price(row.get("entry_price")):
            open_rows.append(row)
        else:
            invalid_open += 1
    history_rows = []
    invalid_history = 0
    for row in ledger.get("history") or []:
        net_return = number(row.get("net_return_pct"))
        if (
            plausible_perp_price(row.get("entry_price"))
            and plausible_perp_price(
                row.get("exit_price"),
                anchor=row.get("entry_price"),
            )
            and math.isfinite(net_return)
            and abs(net_return) <= 100.0
        ):
            history_rows.append(row)
        else:
            invalid_history += 1
    ledger["open"] = open_rows
    ledger["history"] = history_rows
    prior = ledger.get("sanitization") or {}
    ledger["sanitization"] = {
        "last_checked_at": iso_now(),
        "last_invalid_open_removed": invalid_open,
        "last_invalid_history_removed": invalid_history,
        "total_invalid_open_removed": (
            int(prior.get("total_invalid_open_removed") or 0) + invalid_open
        ),
        "total_invalid_history_removed": (
            int(prior.get("total_invalid_history_removed") or 0) + invalid_history
        ),
    }
    return ledger


def asset_for_market(market):
    ticker = str(market.get("ticker") or "").upper()
    if ticker in ASSET_BY_TICKER:
        return ASSET_BY_TICKER[ticker]
    return ticker.removeprefix("KX").removesuffix("PERP")


def load_portfolio(settings):
    starting = setting_float(settings, "CRYPTO_PERPS_SHADOW_STARTING_BALANCE", 500.0)
    portfolio = read_json(PORTFOLIO_FILE, {})
    portfolio.setdefault("mode", "paper_shadow_only")
    portfolio.setdefault("starting_balance", starting)
    portfolio.setdefault("balance", starting)
    portfolio.setdefault("positions", [])
    portfolio.setdefault("history", [])
    # A requested paper-bankroll change is safe to apply only before the lab has
    # generated any trades. Never rewrite a running experiment's P&L history.
    if not portfolio["positions"] and not portfolio["history"] and number(portfolio.get("starting_balance")) != starting:
        portfolio["starting_balance"] = starting
        portfolio["balance"] = starting
    return portfolio


def load_market_history(markets, settings):
    cache = read_json(CACHE_FILE, {"markets": {}})
    rows = cache.setdefault("markets", {})
    cache_minutes = setting_float(settings, "CRYPTO_PERPS_SHADOW_CACHE_MINUTES", 20.0)
    now = now_utc()
    lookback_days = max(7, min(90, setting_int(settings, "CRYPTO_PERPS_SHADOW_LOOKBACK_DAYS", 30)))
    for market in markets:
        ticker = market.get("ticker")
        cached = rows.get(ticker) or {}
        updated = parse_time(cached.get("updated_at"))
        fresh = updated and (now - updated).total_seconds() < cache_minutes * 60
        if fresh and len(cached.get("candles") or []) >= lookback_days * 20:
            continue
        try:
            end_ts = int(time.time())
            payload = http_json(
                f"markets/{urllib.parse.quote(ticker)}/candlesticks",
                {"start_ts": end_ts - lookback_days * 86400, "end_ts": end_ts, "period_interval": 60},
            )
            funding = http_json("funding_rates/estimate", {"ticker": ticker})
            rows[ticker] = {
                "updated_at": iso_now(),
                "candles": payload.get("candlesticks") or [],
                "funding": funding,
            }
        except Exception as exc:
            log(f"PERPS DATA WARNING ticker={ticker} error={type(exc).__name__}:{exc}")
    write_json(CACHE_FILE, cache)
    return rows


def candle_closes(history):
    values = []
    for candle in history.get("candles") or []:
        price = candle.get("price") or {}
        close = price.get("close") if price.get("close") is not None else price.get("previous")
        if number(close) > 0:
            values.append(number(close))
    return values


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def candle_rows(history):
    """Normalize Kalshi candles for signals and executable walk-forward tests."""
    rows = []
    candles = sorted(history.get("candles") or [], key=lambda row: number(row.get("end_period_ts")))
    for candle in candles:
        price = candle.get("price") or {}
        close = number(price.get("close") if price.get("close") is not None else price.get("previous"))
        if close <= 0:
            continue
        bid, ask = candle.get("bid") or {}, candle.get("ask") or {}
        def executable(side, field):
            value = number(side.get(field))
            return value if value > 0 else close

        rows.append({
            "ts": int(number(candle.get("end_period_ts"))),
            "close": close,
            "bid_open": executable(bid, "open"),
            "bid_close": executable(bid, "close"),
            "ask_open": executable(ask, "open"),
            "ask_close": executable(ask, "close"),
            "volume": number(candle.get("volume_notional_value_dollars")),
            "open_interest": number(candle.get("open_interest_notional_value_dollars")),
        })
    return rows


def ema(values, period):
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1.0)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def log_returns(values):
    return [math.log(values[i] / values[i - 1]) for i in range(1, len(values)) if values[i] > 0 and values[i - 1] > 0]


def rolling_zscore(values, window=72):
    sample = [number(value) for value in values[-window:] if number(value) > 0]
    if len(sample) < 8:
        return 0.0
    deviation = statistics.pstdev(sample)
    return (sample[-1] - statistics.mean(sample)) / deviation if deviation > 1e-12 else 0.0


def technical_features(rows):
    """Regime-aware ensemble using only information available at the signal time."""
    if len(rows) < 24:
        return None
    closes = [row["close"] for row in rows]
    returns = log_returns(closes)
    hourly_vol = max(statistics.pstdev(returns[-72:]) if len(returns) >= 8 else 0.0, 0.0005)

    def momentum(hours):
        return math.log(closes[-1] / closes[-1 - hours]) if len(closes) > hours else 0.0

    m1, m4, m12, m24 = momentum(1), momentum(4), momentum(12), momentum(24)
    normalized_momentum = (
        0.10 * clamp(m1 / hourly_vol, -2.5, 2.5) / 2.5
        + 0.35 * clamp(m4 / (hourly_vol * 2.0), -2.5, 2.5) / 2.5
        + 0.35 * clamp(m12 / (hourly_vol * math.sqrt(12.0)), -2.5, 2.5) / 2.5
        + 0.20 * clamp(m24 / (hourly_vol * math.sqrt(24.0)), -2.5, 2.5) / 2.5
    )
    fast, slow = ema(closes[-72:], 6), ema(closes[-144:], 18)
    ema_component = clamp(math.log(max(fast, 1e-9) / max(slow, 1e-9)) / (hourly_vol * 2.0), -1.0, 1.0)
    channel = closes[-25:-1]
    channel_low, channel_high = min(channel), max(channel)
    breakout = clamp(((closes[-1] - channel_low) / max(channel_high - channel_low, 1e-12) - 0.5) * 2.0)
    path = closes[-13:]
    path_distance = sum(abs(path[index] - path[index - 1]) for index in range(1, len(path)))
    efficiency = abs(path[-1] - path[0]) / path_distance if path_distance > 1e-12 else 0.0
    trend_regime = clamp((efficiency - 0.20) / 0.50, 0.0, 1.0)
    trend_core = 0.50 * normalized_momentum + 0.30 * ema_component + 0.20 * breakout
    mean_reversion = -clamp(math.log(closes[-1] / max(slow, 1e-9)) / (hourly_vol * math.sqrt(6.0)), -2.5, 2.5) / 2.5
    short_reversal = -(
        0.45 * clamp(m1 / hourly_vol, -2.5, 2.5) / 2.5
        + 0.55 * clamp(m4 / (hourly_vol * 2.0), -2.5, 2.5) / 2.5
    )
    technical = trend_regime * trend_core + (1.0 - trend_regime) * (0.70 * trend_core + 0.30 * mean_reversion)

    volumes = [row["volume"] for row in rows]
    interests = [row["open_interest"] for row in rows if row["open_interest"] > 0]
    volume_z = clamp(rolling_zscore(volumes) / 3.0)
    oi_change = math.log(interests[-1] / interests[-13]) if len(interests) >= 13 and interests[-13] > 0 else 0.0
    flow_strength = 0.55 * max(0.0, volume_z) + 0.45 * clamp(oi_change / 0.05)
    flow_confirmation = (1.0 if technical >= 0 else -1.0) * flow_strength

    changes = [closes[index] - closes[index - 1] for index in range(max(1, len(closes) - 14), len(closes))]
    avg_gain = statistics.mean(max(change, 0.0) for change in changes) if changes else 0.0
    avg_loss = statistics.mean(max(-change, 0.0) for change in changes) if changes else 0.0
    rsi = 100.0 if avg_loss <= 1e-12 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    rsi_reversion = clamp((50.0 - rsi) / 25.0)
    band_sample = closes[-20:]
    band_mean = statistics.mean(band_sample)
    band_deviation = statistics.pstdev(band_sample)
    bollinger_z = (closes[-1] - band_mean) / band_deviation if band_deviation > 1e-12 else 0.0
    range_reversion = (1.0 - trend_regime) * (0.55 * -clamp(bollinger_z / 2.0) + 0.45 * rsi_reversion)
    short_vol = statistics.pstdev(returns[-12:]) if len(returns) >= 12 else hourly_vol
    squeeze = clamp((hourly_vol / max(short_vol, 0.00025) - 1.0) / 1.5, 0.0, 1.0)
    breakout_volume = breakout * max(0.0, volume_z) * (0.50 + 0.50 * squeeze)
    shock = clamp((abs(m1) / hourly_vol - 2.0) / 3.0, 0.0, 1.0)
    liquidation_reversal = -(1.0 if m1 >= 0 else -1.0) * shock * max(0.0, volume_z) * (0.50 + 0.50 * clamp(oi_change / 0.05, 0.0, 1.0))
    macro_fast, macro_slow = ema(closes[-240:], 72), ema(closes[-480:], 240)
    macro_trend = clamp(math.log(max(macro_fast, 1e-9) / max(macro_slow, 1e-9)) / (hourly_vol * 4.0))
    macro_direction = 1.0 if macro_trend >= 0 else -1.0
    pullback_quality = max(0.0, macro_direction * short_reversal)
    trend_pullback = macro_trend * clamp(0.25 + pullback_quality, 0.0, 1.0)
    return {
        "technical": technical, "trend_core": trend_core, "flow_confirmation": flow_confirmation,
        "mean_reversion": mean_reversion, "short_reversal": short_reversal, "ema_component": ema_component,
        "momentum_1h": m1, "momentum_4h": m4, "momentum_12h": m12, "momentum_24h": m24,
        "hourly_vol": hourly_vol, "ema_fast": fast, "ema_slow": slow,
        "breakout_score": breakout, "trend_efficiency": efficiency, "trend_regime": trend_regime,
        "volume_zscore": rolling_zscore(volumes), "open_interest_change_12h": oi_change,
        "rsi": rsi, "bollinger_zscore": bollinger_z, "range_reversion": range_reversion,
        "volatility_squeeze": squeeze, "breakout_volume": breakout_volume,
        "liquidation_reversal": liquidation_reversal, "macro_trend": macro_trend,
        "trend_pullback": trend_pullback,
    }


def model_raw_scores(features):
    trend = 2.5 * (0.87 * features["technical"] + 0.13 * features["flow_confirmation"])
    reversal = 2.5 * (
        (1.0 - 0.70 * features["trend_regime"])
        * (0.65 * features["short_reversal"] + 0.35 * features["mean_reversion"])
    )
    return {
        "trend": trend,
        "reversal": reversal,
        "range_reversion": 2.5 * features["range_reversion"],
        "volume_breakout": 2.5 * features["breakout_volume"],
        "liquidation_reversal": 2.5 * features["liquidation_reversal"],
        "trend_pullback": 2.5 * features["trend_pullback"],
    }


def walk_forward_validation(history, settings):
    """Like-for-like ensemble replay with executable prices, stops, targets, and fees."""
    rows = candle_rows(history)
    max_hold = max(1, min(48, setting_int(settings, "CRYPTO_PERPS_SHADOW_MAX_HOLD_HOURS", 24)))
    fee_rate = setting_float(settings, "CRYPTO_PERPS_SHADOW_FEE_RATE", 0.0005)
    min_score = setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_SIGNAL_SCORE", 1.0)
    reward_risk = setting_float(settings, "CRYPTO_PERPS_SHADOW_REWARD_RISK", 1.8)
    returns = []
    exit_reasons = {}
    index = 47
    while index < len(rows) - 2:
        features = technical_features(rows[: index + 1])
        if not features:
            index += 1
            continue
        # Funding and current-basis history are unavailable per candle. Renormalize
        # the exact technical + flow components used by the live ensemble.
        raw = 2.5 * (0.70 * features["technical"] + 0.13 * features["flow_confirmation"]) / 0.83
        direction = 1 if raw > 0 else -1
        entry_row = rows[index + 1]
        entry = entry_row["ask_open"] if direction > 0 else entry_row["bid_open"]
        spread_pct = max(0.0, entry_row["ask_open"] - entry_row["bid_open"]) / max(entry_row["close"], 1e-12)
        score = abs(raw) - spread_pct * 120.0
        if score < min_score or entry <= 0:
            index += 1
            continue
        stop_pct = min(0.06, max(0.012, features["hourly_vol"] * math.sqrt(6.0) * 1.8))
        target_pct = stop_pct * reward_risk
        last_index = min(len(rows) - 1, index + 1 + max_hold)
        exit_index, exit_price, reason = last_index, 0.0, "time_exit"
        for cursor in range(index + 1, last_index + 1):
            row = rows[cursor]
            executable_close = row["bid_close"] if direction > 0 else row["ask_close"]
            move = direction * (executable_close / entry - 1.0)
            if move <= -stop_pct:
                exit_index, exit_price, reason = cursor, executable_close, "stop_loss"
                break
            if move >= target_pct:
                exit_index, exit_price, reason = cursor, executable_close, "take_profit"
                break
            exit_price = executable_close
        if entry <= 0 or exit_price <= 0 or abs(math.log(exit_price / entry)) > 0.25:
            index += 1
            continue
        returns.append(direction * (exit_price / entry - 1.0) - 2.0 * fee_rate)
        exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
        index = max(index + 1, exit_index)
    wins = sum(value > 0 for value in returns)
    losses = sum(value <= 0 for value in returns)
    gross_win = sum(value for value in returns if value > 0)
    gross_loss = abs(sum(value for value in returns if value < 0))
    trades = len(returns)
    win_rate = wins / trades if trades else 0.0
    avg_net = statistics.mean(returns) if returns else 0.0
    min_trades = setting_int(settings, "CRYPTO_PERPS_SHADOW_MIN_VALIDATION_TRADES", 30)
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (99.0 if gross_win > 0 else 0.0)
    passing = trades >= min_trades and (
        avg_net > setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_VALIDATION_AVG_NET", 0.0)
        and profit_factor >= setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_VALIDATION_PROFIT_FACTOR", 1.05)
    )
    return {
        "trades": trades, "wins": wins, "losses": losses, "win_rate": win_rate,
        "avg_net_return": avg_net, "profit_factor": profit_factor,
        "horizon_hours": max_hold, "max_hold_hours": max_hold,
        "exit_reasons": exit_reasons, "strategy": "ensemble_stop_target_time",
        "enough_data": trades >= min_trades, "passing": passing,
    }


def historical_model_replay(history, settings):
    """Replay every specialist without lookahead; exploratory, never an entry permission."""
    rows = candle_rows(history)
    horizon = max(1, min(12, setting_int(settings, "CRYPTO_PERPS_SHADOW_VALIDATION_HOURS", 4)))
    fee_rate = setting_float(settings, "CRYPTO_PERPS_SHADOW_FEE_RATE", 0.0005)
    threshold = setting_float(settings, "CRYPTO_PERPS_SHADOW_FORECAST_MIN_SCORE", 0.5)
    outcomes = {}
    for index in range(47, len(rows) - horizon - 1, horizon):
        features = technical_features(rows[: index + 1])
        if not features:
            continue
        for model, score in model_raw_scores(features).items():
            if abs(score) < threshold:
                continue
            direction = 1 if score > 0 else -1
            entry_row, exit_row = rows[index + 1], rows[index + horizon]
            entry = entry_row["ask_open"] if direction > 0 else entry_row["bid_open"]
            exit_price = exit_row["bid_close"] if direction > 0 else exit_row["ask_close"]
            if entry <= 0 or exit_price <= 0 or abs(math.log(exit_price / entry)) > 0.25:
                continue
            outcomes.setdefault(model, []).append(direction * (exit_price / entry - 1.0) - 2.0 * fee_rate)
    report = {}
    for model, values in outcomes.items():
        wins = sum(value > 0 for value in values)
        report[model] = {
            "trades": len(values), "wins": wins, "losses": len(values) - wins,
            "win_rate": round(wins / len(values) * 100.0, 1) if values else 0.0,
            "avg_net_pct": round(statistics.mean(values) * 100.0, 5) if values else 0.0,
        }
    return report


def build_candidate(market, history, settings):
    rows = candle_rows(history)
    features = technical_features(rows)
    if not features:
        return None
    bid, ask = number(market.get("bid")), number(market.get("ask"))
    mark = market_price(market, "settlement_mark_price") or number(market.get("price"))
    if not executable_market_valid(market) or ask <= bid:
        return None
    m1, m4, m12, m24 = (features[key] for key in ("momentum_1h", "momentum_4h", "momentum_12h", "momentum_24h"))
    hourly_vol, fast, slow = features["hourly_vol"], features["ema_fast"], features["ema_slow"]
    reference = market_price(market, "reference_price")
    basis_pct = math.log(mark / reference) if reference > 0 else 0.0
    basis_component = -clamp(basis_pct / max(hourly_vol * 2.0, 0.001))
    funding = history.get("funding") or {}
    funding_rate = number(funding.get("funding_rate"), 0.0)
    funding_component = -clamp(funding_rate / 0.002)
    standalone_models = model_raw_scores(features)
    standalone_models["funding_contrarian"] = 2.5 * (0.70 * funding_component + 0.30 * basis_component)
    ensemble = (
        0.70 * features["technical"]
        + 0.13 * features["flow_confirmation"]
        + 0.12 * basis_component
        + 0.05 * funding_component
    )
    raw_score = 2.5 * ensemble
    direction = 1 if raw_score >= 0 else -1
    side = "long" if direction > 0 else "short"
    entry = ask if direction > 0 else bid
    spread_pct = (ask - bid) / mark
    funding_cost = max(0.0, direction * funding_rate)
    score = abs(raw_score) - spread_pct * 120.0 - funding_cost * 40.0
    aligned_votes = sum(value * direction > 0 for value in (m4, m12, features["trend_core"]))
    aligned = aligned_votes >= 2
    validation = walk_forward_validation(history, settings)
    model_replay = historical_model_replay(history, settings)
    volume_24h = number(market.get("volume_24h_notional_value_dollars"))
    min_volume = setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_VOLUME_24H", 100000.0)
    max_spread = setting_float(settings, "CRYPTO_PERPS_SHADOW_MAX_SPREAD_PCT", 0.0025)
    min_score = setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_SIGNAL_SCORE", 1.0)
    bayesian_win_rate = (validation["wins"] + 10.0) / (validation["trades"] + 20.0)
    validation_adjustment = (bayesian_win_rate - 0.50) * 30.0 + (3.0 if validation["avg_net_return"] > 0 else -3.0)
    confidence = max(0.0, min(99.0, 50.0 + score * 13.0 + (7.0 if aligned else -10.0) + validation_adjustment - spread_pct * 2500.0))
    reasons = []
    if not aligned:
        reasons.append("timeframes_not_aligned")
    if score < min_score:
        reasons.append("weak_signal")
    if spread_pct > max_spread:
        reasons.append("spread_too_wide")
    if volume_24h < min_volume:
        reasons.append("low_volume")
    if confidence < setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_CONFIDENCE", 68.0):
        reasons.append("low_confidence")
    if not validation["enough_data"]:
        reasons.append("historical_validation_insufficient")
    elif not validation["passing"]:
        reasons.append("historical_validation_weak")
    stop_pct = min(0.06, max(0.012, hourly_vol * math.sqrt(6.0) * 1.8))
    target_pct = stop_pct * setting_float(settings, "CRYPTO_PERPS_SHADOW_REWARD_RISK", 1.8)
    return {
        "ticker": market.get("ticker"), "asset": asset_for_market(market), "side": side,
        "direction": direction, "entry_price": round(entry, 6), "mark_price": round(mark, 6),
        "bid": bid, "ask": ask, "spread_pct": round(spread_pct, 6),
        "volume_24h": round(volume_24h, 2), "funding_rate": funding_rate,
        "reference_price": round(reference, 6), "basis_pct": round(basis_pct, 6),
        "signal_score": round(score, 4), "raw_score": round(raw_score, 4),
        "confidence": round(confidence, 1), "momentum_1h": round(m1, 6),
        "momentum_4h": round(m4, 6), "momentum_12h": round(m12, 6),
        "momentum_24h": round(m24, 6), "hourly_vol": round(hourly_vol, 6),
        "ema_fast": round(fast, 6), "ema_slow": round(slow, 6),
        "breakout_score": round(features["breakout_score"], 4),
        "trend_efficiency": round(features["trend_efficiency"], 4),
        "trend_regime": round(features["trend_regime"], 4),
        "volume_zscore": round(features["volume_zscore"], 4),
        "open_interest_change_12h": round(features["open_interest_change_12h"], 6),
        "rsi": round(features["rsi"], 2), "bollinger_zscore": round(features["bollinger_zscore"], 4),
        "volatility_squeeze": round(features["volatility_squeeze"], 4),
        "validation_trades": validation["trades"],
        "validation_win_rate": round(validation["win_rate"] * 100.0, 1),
        "validation_avg_net_pct": round(validation["avg_net_return"] * 100.0, 4),
        "validation_profit_factor": round(validation["profit_factor"], 3),
        "validation_passing": validation["passing"],
        "validation_enough_data": validation["enough_data"],
        "validation_strategy": validation["strategy"],
        "historical_model_replay": model_replay,
        "model_scores": {key: round(value, 4) for key, value in standalone_models.items()} | {"ensemble": round(raw_score, 4)},
        "stop_pct": round(stop_pct, 5), "target_pct": round(target_pct, 5),
        "timeframes_aligned": aligned,
        "eligibility_lane": "validated" if not reasons else "blocked",
        "eligible": not reasons, "skip_reasons": reasons,
    }


def apply_cross_market_context(candidates, settings):
    """Add relative momentum and broad-market confirmation without hard coupling assets."""
    if not candidates:
        return candidates
    median_m12 = statistics.median(number(row.get("momentum_12h")) for row in candidates)
    btc = next((row for row in candidates if row.get("asset") == "BTC"), None)
    btc_direction = number(btc.get("direction")) if btc else 0.0
    min_score = setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_SIGNAL_SCORE", 1.0)
    min_confidence = setting_float(settings, "CRYPTO_PERPS_SHADOW_MIN_CONFIDENCE", 68.0)
    for row in candidates:
        direction = number(row.get("direction"))
        scale = max(number(row.get("hourly_vol")) * math.sqrt(12.0), 0.002)
        relative = clamp((number(row.get("momentum_12h")) - median_m12) / scale)
        relative_alignment = direction * relative
        broad_adjustment = 0.0 if not btc_direction or row.get("asset") == "BTC" else (2.0 if direction == btc_direction else -3.0)
        score = number(row.get("signal_score")) + 0.10 * relative_alignment
        confidence = clamp(number(row.get("confidence")) + 4.0 * relative_alignment + broad_adjustment, 0.0, 99.0)
        reasons = [reason for reason in (row.get("skip_reasons") or []) if reason not in {"weak_signal", "low_confidence"}]
        if score < min_score:
            reasons.append("weak_signal")
        if confidence < min_confidence:
            reasons.append("low_confidence")
        row.update({
            "cross_sectional_momentum": round(relative, 4),
            "btc_regime_aligned": not btc_direction or row.get("asset") == "BTC" or direction == btc_direction,
            "signal_score": round(score, 4), "confidence": round(confidence, 1),
            "eligibility_lane": "validated" if not reasons else "blocked",
            "eligible": not reasons, "skip_reasons": reasons,
        })
    return candidates


def apply_paper_probation(candidates, settings):
    """Admit only strong current setups to a tiny, one-position paper learning lane."""
    if not setting_bool(settings, "CRYPTO_PERPS_SHADOW_PROBATION_ENABLED", True):
        return candidates
    validation_reasons = {"historical_validation_weak", "historical_validation_insufficient"}
    min_score = setting_float(settings, "CRYPTO_PERPS_SHADOW_PROBATION_MIN_SIGNAL_SCORE", 1.5)
    min_confidence = setting_float(settings, "CRYPTO_PERPS_SHADOW_PROBATION_MIN_CONFIDENCE", 78.0)
    max_spread = setting_float(settings, "CRYPTO_PERPS_SHADOW_PROBATION_MAX_SPREAD_PCT", 0.001)
    for row in candidates:
        reasons = list(row.get("skip_reasons") or [])
        non_validation_reasons = [reason for reason in reasons if reason not in validation_reasons]
        qualifies = (
            bool(validation_reasons.intersection(reasons))
            and not non_validation_reasons
            and bool(row.get("timeframes_aligned"))
            and number(row.get("signal_score")) >= min_score
            and number(row.get("confidence")) >= min_confidence
            and number(row.get("spread_pct")) <= max_spread
        )
        row["probation"] = {
            "enabled": True,
            "qualified": qualifies,
            "paper_only": True,
            "reason": "collect_forward_profitability_evidence" if qualifies else "thresholds_not_met",
        }
        if qualifies:
            row["eligible"] = True
            row["eligibility_lane"] = "paper_probation"
            row["skip_reasons"] = []
            row["probation_validation_reasons"] = [reason for reason in reasons if reason in validation_reasons]
    return candidates


def accrue_funding(position, market, history, portfolio):
    last = parse_time(position.get("funding_updated_at")) or parse_time(position.get("opened_at")) or now_utc()
    elapsed_hours = max(0.0, min(1.0, (now_utc() - last).total_seconds() / 3600.0))
    rate = number((history.get("funding") or {}).get("funding_rate"), 0.0)
    mark = market_price(market, "settlement_mark_price")
    contracts = number(position.get("contracts"))
    if (not plausible_perp_price(mark) or not math.isfinite(rate) or abs(rate) > 1
            or not math.isfinite(contracts) or contracts <= 0
            or number(position.get("direction")) not in (-1, 1)):
        return False
    notional = abs(contracts * mark)
    payment = -number(position.get("direction")) * notional * rate * elapsed_hours / 8.0
    if payment:
        portfolio["balance"] = round(number(portfolio.get("balance")) + payment, 6)
        position["funding_pnl"] = round(number(position.get("funding_pnl")) + payment, 6)
    position["funding_updated_at"] = iso_now()
    position["last_funding_rate"] = rate
    return True


def close_positions(portfolio, markets_by_ticker, histories, candidates_by_ticker, settings):
    closed = []
    fee_rate = setting_float(settings, "CRYPTO_PERPS_SHADOW_FEE_RATE", 0.0005)
    max_hours = setting_float(settings, "CRYPTO_PERPS_SHADOW_MAX_HOLD_HOURS", 24.0)
    for position in portfolio.get("positions") or []:
        if position.get("status") != "open":
            continue
        market = markets_by_ticker.get(position.get("ticker"))
        if not market:
            continue
        history = histories.get(position.get("ticker")) or {}
        direction = number(position.get("direction"))
        exit_price = market_price(market, "bid" if direction > 0 else "ask")
        entry = number(position.get("entry_price"))
        contracts = number(position.get("contracts"))
        if (not executable_market_valid(market) or not plausible_perp_price(entry)
                or direction not in (-1, 1) or not math.isfinite(contracts) or contracts <= 0):
            if position.get("data_quality_block") != "invalid_execution_price_or_position":
                event({"type": "perps_shadow_quote_rejected", "position_id": position.get("id"),
                       "ticker": position.get("ticker"), "bid": market.get("bid"),
                       "ask": market.get("ask"), "mark": market.get("settlement_mark_price")})
            position["data_quality_block"] = "invalid_execution_price_or_position"
            continue
        position.pop("data_quality_block", None)
        if not accrue_funding(position, market, history, portfolio):
            position["data_quality_block"] = "invalid_funding_input"
        move = direction * (exit_price / entry - 1.0)
        max_favorable = max(number(position.get("max_favorable_move")), move)
        position["max_favorable_move"] = round(max_favorable, 6)
        trail_activation = number(position.get("stop_pct")) * setting_float(settings, "CRYPTO_PERPS_SHADOW_TRAIL_ACTIVATION_R", 0.75)
        entry_vol = number((position.get("entry_snapshot") or {}).get("hourly_vol"), 0.004)
        trail_gap = max(0.004, entry_vol * setting_float(settings, "CRYPTO_PERPS_SHADOW_TRAIL_VOL_MULTIPLIER", 1.5))
        trailing_floor = max(0.0, max_favorable - trail_gap)
        opened = parse_time(position.get("opened_at")) or now_utc()
        held_hours = max(0.0, (now_utc() - opened).total_seconds() / 3600.0)
        candidate = candidates_by_ticker.get(position.get("ticker")) or {}
        reason = ""
        if move <= -number(position.get("stop_pct")):
            reason = "stop_loss"
        elif move >= number(position.get("target_pct")):
            reason = "take_profit"
        elif max_favorable >= trail_activation and move <= trailing_floor:
            reason = "trailing_stop"
        elif held_hours >= max_hours:
            reason = "time_exit"
        elif candidate and number(candidate.get("direction")) == -direction and number(candidate.get("signal_score")) >= 1.0:
            reason = "signal_reversal"
        if not reason:
            position["mark_price"] = market_price(market, "settlement_mark_price")
            position["unrealized_pnl"] = round(direction * (exit_price - entry) * number(position.get("contracts")), 6)
            continue
        gross = direction * (exit_price - entry) * number(position.get("contracts"))
        exit_fee = abs(exit_price * number(position.get("contracts"))) * fee_rate
        portfolio["balance"] = round(number(portfolio.get("balance")) + gross - exit_fee, 6)
        net = gross - number(position.get("entry_fee")) - exit_fee + number(position.get("funding_pnl"))
        position.update({
            "status": "settled", "closed_at": iso_now(), "exit_price": round(exit_price, 6),
            "exit_reason": reason, "held_hours": round(held_hours, 2), "gross_price_pnl": round(gross, 6),
            "exit_fee": round(exit_fee, 6), "profit": round(net, 6),
            "result": "WIN" if net > 0 else "LOSS", "unrealized_pnl": 0.0,
        })
        closed.append(position)
        log(f"PERPS SHADOW CLOSE {position['asset']} {position['side']} reason={reason} pnl=${net:.4f}")
        event({"type": "perps_shadow_close", "position": position})
    if closed:
        portfolio["history"].extend(closed)
        portfolio["positions"] = [row for row in portfolio["positions"] if row.get("status") == "open"]
    return closed


def open_positions(portfolio, candidates, settings):
    opened = []
    max_open = setting_int(settings, "CRYPTO_PERPS_SHADOW_MAX_OPEN", 3)
    notional_pct = setting_float(settings, "CRYPTO_PERPS_SHADOW_NOTIONAL_PCT", 0.02)
    max_notional = setting_float(settings, "CRYPTO_PERPS_SHADOW_MAX_NOTIONAL", 25.0)
    max_same_direction = setting_int(settings, "CRYPTO_PERPS_SHADOW_MAX_SAME_DIRECTION", 2)
    fee_rate = setting_float(settings, "CRYPTO_PERPS_SHADOW_FEE_RATE", 0.0005)
    for row in portfolio.get("positions") or []:
        snapshot = row.get("entry_snapshot") or {}
        legacy_validation_weak = (
            snapshot.get("validation_passing") is False
            or number(snapshot.get("validation_avg_net_pct")) <= 0
            or number(snapshot.get("validation_profit_factor")) < 1.05
        )
        if row.get("status") == "open" and not row.get("eligibility_lane") and legacy_validation_weak:
            row["eligibility_lane"] = "paper_probation"
            row["legacy_pre_probation_sizing"] = True
        # Existing fills retain their original economics. New probation caps
        # apply only to future positions; migration never refunds an entry fee.
    open_assets = {row.get("asset") for row in portfolio.get("positions") or [] if row.get("status") == "open"}
    probation_open = sum(
        row.get("eligibility_lane") == "paper_probation"
        for row in portfolio.get("positions") or []
        if row.get("status") == "open"
    )
    probation_max_open = setting_int(settings, "CRYPTO_PERPS_SHADOW_PROBATION_MAX_OPEN", 1)
    for candidate in sorted(candidates, key=lambda row: (row.get("signal_score", 0), row.get("confidence", 0)), reverse=True):
        if len(portfolio.get("positions") or []) >= max_open:
            break
        if not candidate.get("eligible") or candidate.get("asset") in open_assets:
            continue
        is_probation = candidate.get("eligibility_lane") == "paper_probation"
        if is_probation and probation_open >= probation_max_open:
            continue
        same_direction = sum(number(row.get("direction")) == number(candidate.get("direction")) for row in portfolio.get("positions") or [])
        if same_direction >= max_same_direction:
            continue
        candidate_notional_pct = (
            setting_float(settings, "CRYPTO_PERPS_SHADOW_PROBATION_NOTIONAL_PCT", 0.0025)
            if is_probation else notional_pct
        )
        candidate_max_notional = (
            setting_float(settings, "CRYPTO_PERPS_SHADOW_PROBATION_MAX_NOTIONAL", 2.5)
            if is_probation else max_notional
        )
        base_notional = number(portfolio.get("balance")) * candidate_notional_pct
        quality_scale = clamp(0.75 + max(0.0, number(candidate.get("signal_score")) - 0.8) * 0.25, 0.75, 1.25)
        volatility_scale = clamp(0.008 / max(number(candidate.get("hourly_vol")), 0.0005), 0.60, 1.20)
        notional = min(candidate_max_notional, base_notional * quality_scale * volatility_scale)
        if notional < (1.0 if is_probation else 5.0):
            continue
        entry = number(candidate.get("entry_price"))
        if (not plausible_perp_price(candidate.get("mark_price"))
                or not plausible_perp_price(entry, anchor=candidate.get("mark_price"))
                or number(candidate.get("direction")) not in (-1, 1)):
            continue
        contracts = notional / entry
        entry_fee = notional * fee_rate
        if number(portfolio.get("balance")) - sum(number(row.get("margin")) for row in portfolio.get("positions") or []) < notional + entry_fee:
            continue
        position = {
            "id": uuid.uuid4().hex, "status": "open", "mode": "paper_shadow_only",
            "ticker": candidate.get("ticker"), "asset": candidate.get("asset"),
            "side": candidate.get("side"), "direction": candidate.get("direction"),
            "opened_at": iso_now(), "funding_updated_at": iso_now(),
            "entry_price": entry, "mark_price": candidate.get("mark_price"),
            "notional": round(notional, 6), "margin": round(notional, 6), "leverage": 1.0,
            "contracts": round(contracts, 6), "entry_fee": round(entry_fee, 6),
            "funding_pnl": 0.0, "unrealized_pnl": 0.0,
            "stop_pct": candidate.get("stop_pct"), "target_pct": candidate.get("target_pct"),
            "max_favorable_move": 0.0,
            "signal_score": candidate.get("signal_score"), "confidence": candidate.get("confidence"),
            "eligibility_lane": candidate.get("eligibility_lane", "validated"),
            "sizing_multiplier": round(quality_scale * volatility_scale, 4),
            "entry_snapshot": candidate,
        }
        portfolio["balance"] = round(number(portfolio.get("balance")) - entry_fee, 6)
        portfolio["positions"].append(position)
        open_assets.add(position["asset"])
        opened.append(position)
        if is_probation:
            probation_open += 1
        log(f"PERPS SHADOW OPEN {position['asset']} {position['side']} notional=${notional:.2f} entry={entry:.6f} score={position['signal_score']}")
        event({"type": "perps_shadow_open", "position": position})
    return opened


def update_forward_forecasts(candidates, markets_by_ticker, settings):
    """Track competing algorithms forward without opening even a paper position."""
    ledger = sanitize_forecast_ledger(
        read_json(
            FORECASTS_FILE,
            {"mode": "research_forecasts_only", "open": [], "history": []},
        )
    )
    ledger.setdefault("open", [])
    ledger.setdefault("history", [])
    fee_rate = setting_float(settings, "CRYPTO_PERPS_SHADOW_FEE_RATE", 0.0005)
    now = now_utc()
    still_open = []
    for forecast in ledger["open"]:
        due = parse_time(forecast.get("due_at"))
        market = markets_by_ticker.get(forecast.get("ticker"))
        if not due or now < due or not market:
            still_open.append(forecast)
            continue
        direction = number(forecast.get("direction"))
        exit_price = number(market.get("bid")) if direction > 0 else number(market.get("ask"))
        entry = number(forecast.get("entry_price"))
        if (
            not plausible_perp_price(entry)
            or not plausible_perp_price(exit_price, anchor=entry)
        ):
            still_open.append(forecast)
            continue
        net_return = direction * (exit_price / entry - 1.0) - 2.0 * fee_rate
        forecast.update({
            "status": "settled", "settled_at": iso_now(), "exit_price": round(exit_price, 6),
            "net_return_pct": round(net_return * 100.0, 5), "result": "WIN" if net_return > 0 else "LOSS",
        })
        ledger["history"].append(forecast)
    ledger["open"] = still_open

    min_forecast_score = setting_float(settings, "CRYPTO_PERPS_SHADOW_FORECAST_MIN_SCORE", 0.5)
    existing = {(row.get("ticker"), row.get("model"), row.get("horizon_hours")) for row in ledger["open"]}
    for candidate in candidates:
        market = markets_by_ticker.get(candidate.get("ticker")) or {}
        for model, score in (candidate.get("model_scores") or {}).items():
            if abs(number(score)) < min_forecast_score:
                continue
            direction = 1 if number(score) > 0 else -1
            entry = number(market.get("ask")) if direction > 0 else number(market.get("bid"))
            mark = market_price(market, "settlement_mark_price") or number(
                market.get("price")
            )
            if not plausible_perp_price(entry, anchor=mark):
                continue
            for horizon in (1, 4):
                key = (candidate.get("ticker"), model, horizon)
                if key in existing:
                    continue
                due = datetime.fromtimestamp(now.timestamp() + horizon * 3600, timezone.utc).isoformat(timespec="seconds")
                ledger["open"].append({
                    "id": uuid.uuid4().hex, "status": "open", "ticker": candidate.get("ticker"),
                    "asset": candidate.get("asset"), "model": model, "horizon_hours": horizon,
                    "opened_at": iso_now(), "due_at": due, "direction": direction,
                    "side": "long" if direction > 0 else "short", "entry_price": round(entry, 6),
                    "signal_score": round(abs(number(score)), 4),
                })
                existing.add(key)
    # Forecast counts are cumulative; reporting limits must not erase evidence.
    ledger["retention_method"] = "cumulative_forecast_history"
    write_json(FORECASTS_FILE, ledger)
    return ledger


def summarize_forecasts(ledger):
    ledger = sanitize_forecast_ledger(ledger)
    groups = {}
    for row in ledger.get("history") or []:
        key = f"{row.get('model')}:{row.get('horizon_hours')}h"
        group = groups.setdefault(key, {"model": row.get("model"), "horizon_hours": row.get("horizon_hours"), "settled": 0, "wins": 0, "net_total": 0.0})
        group["settled"] += 1
        group["wins"] += row.get("result") == "WIN"
        group["net_total"] += number(row.get("net_return_pct"))
    rows = []
    for group in groups.values():
        settled = group.pop("settled")
        wins = group.pop("wins")
        net_total = group.pop("net_total")
        rows.append({**group, "settled": settled, "wins": wins, "losses": settled - wins,
                     "win_rate": round(wins / settled * 100.0, 1) if settled else 0.0,
                     "avg_net_pct": round(net_total / settled, 5) if settled else 0.0})
    return {"open_count": len(ledger.get("open") or []), "settled_count": len(ledger.get("history") or []),
            "sanitization": ledger.get("sanitization") or {},
            "models": sorted(rows, key=lambda row: (row["horizon_hours"], row["model"])),
            "recent": (ledger.get("history") or [])[-50:]}


def summarize(portfolio, markets, candidates, opened, closed, settings, error=""):
    open_rows = [row for row in portfolio.get("positions") or [] if row.get("status") == "open"]
    raw_history = portfolio.get("history") or []
    invalid_history = [row for row in raw_history if invalid_settlement_reason(row)]
    history = [row for row in raw_history if not invalid_settlement_reason(row)]
    if invalid_history:
        warning = f"{len(invalid_history)} invalid paper settlement(s) retained but unscored; performance is incomplete"
        error = f"{error}; {warning}" if error else warning
    unrealized = sum(number(row.get("unrealized_pnl")) for row in open_rows)
    realized = sum(number(row.get("profit")) for row in history)
    portfolio_wins = sum(row.get("result") == "WIN" for row in history)
    portfolio_losses = sum(row.get("result") == "LOSS" for row in history)
    balance = number(portfolio.get("balance"))
    validated = [row for row in candidates if number(row.get("validation_trades")) >= setting_int(settings, "CRYPTO_PERPS_SHADOW_MIN_VALIDATION_TRADES", 30)]
    validation_summary = {
        "assets_validated": len(validated),
        "assets_passing": sum(bool(row.get("validation_passing")) for row in validated),
        "median_win_rate": round(statistics.median(number(row.get("validation_win_rate")) for row in validated), 1) if validated else 0.0,
        "median_avg_net_pct": round(statistics.median(number(row.get("validation_avg_net_pct")) for row in validated), 4) if validated else 0.0,
        "method": "non_overlapping_ensemble_stop_target_time_after_spread_and_fees",
    }
    historical_models = {}
    for candidate in candidates:
        for model, result in (candidate.get("historical_model_replay") or {}).items():
            group = historical_models.setdefault(model, {"model": model, "trades": 0, "wins": 0, "net_sum": 0.0, "profitable_assets": 0})
            trades = int(number(result.get("trades")))
            group["trades"] += trades
            group["wins"] += int(number(result.get("wins")))
            group["net_sum"] += number(result.get("avg_net_pct")) * trades
            group["profitable_assets"] += trades > 0 and number(result.get("avg_net_pct")) > 0
    historical_model_summary = []
    for group in historical_models.values():
        trades = group.pop("trades")
        model_wins = group.pop("wins")
        net_sum = group.pop("net_sum")
        historical_model_summary.append({**group, "trades": trades, "wins": model_wins, "losses": trades - model_wins,
                                         "win_rate": round(model_wins / trades * 100.0, 1) if trades else 0.0,
                                         "avg_net_pct": round(net_sum / trades, 5) if trades else 0.0})
    report = {
        "generated_at": iso_now(), "mode": "paper_shadow_only",
        "execution_enabled": setting_bool(settings, "CRYPTO_PERPS_SHADOW_EXECUTION_ENABLED", False),
        "error": error, "starting_balance": number(portfolio.get("starting_balance")),
        "balance": round(balance, 2), "equity": round(balance + unrealized, 2),
        "cash_reconciliation": {
            "method": "immutable_position_economics_no_silent_balance_reset",
            "residual_dollars": round(balance - math.fsum([
                number(portfolio.get("starting_balance")), realized,
                *(number(row.get("funding_pnl")) - number(row.get("entry_fee"))
                  for row in open_rows + invalid_history)]), 8),
            "status": "excludes_invalid_exits_retains_known_entry_fees_and_funding",
        },
        "data_integrity": {
            "status": "incomplete" if invalid_history else "ok",
            "unscored_settlements": len(invalid_history),
            "unscored_ids": [row.get("id") for row in invalid_history],
            "blocked_open_positions": sum(bool(row.get("data_quality_block")) for row in open_rows),
            "repair": portfolio.get("invalid_settlement_repair"),
        },
        "realized_profit": round(realized, 4), "unrealized_profit": round(unrealized, 4),
        "open_margin": round(sum(number(row.get("margin")) for row in open_rows), 2),
        "market_count": len(markets), "candidate_count": len(candidates),
        "eligible_count": sum(bool(row.get("eligible")) for row in candidates),
        "opened_count": len(opened), "closed_count": len(closed), "open_count": len(open_rows),
        "settled_count": len(history), "wins": portfolio_wins, "losses": portfolio_losses,
        "win_rate": round(portfolio_wins / max(1, portfolio_wins + portfolio_losses) * 100.0, 1),
        "total_funding_pnl": round(sum(number(row.get("funding_pnl")) for row in open_rows + history), 6),
        "top_candidates": sorted(candidates, key=lambda row: row.get("signal_score", -999), reverse=True)[:20],
        "open_positions": open_rows, "recent_history": history[-50:],
        "available_assets": sorted({asset_for_market(row) for row in markets}),
        "validation_summary": validation_summary,
        "historical_model_summary": sorted(historical_model_summary, key=lambda row: row["avg_net_pct"], reverse=True),
        "forward_forecasts": summarize_forecasts(read_json(FORECASTS_FILE, {"open": [], "history": []})),
        "settings": {key: value for key, value in settings.items() if key.startswith("CRYPTO_PERPS_SHADOW_")},
    }
    write_json(REPORT_FILE, report)
    return report


def run_perps_shadow(settings):
    portfolio = load_portfolio(settings)
    if not setting_bool(settings, "CRYPTO_PERPS_SHADOW_ENABLED", True):
        return summarize(portfolio, [], [], [], [], settings, error="disabled")
    try:
        markets = (http_json("markets", {"status": "active"}).get("markets") or [])
        histories = load_market_history(markets, settings)
        candidates = [row for row in (build_candidate(market, histories.get(market.get("ticker"), {}), settings) for market in markets) if row]
        candidates = apply_cross_market_context(candidates, settings)
        candidates = apply_paper_probation(candidates, settings)
        markets_by_ticker = {row.get("ticker"): row for row in markets}
        candidates_by_ticker = {row.get("ticker"): row for row in candidates}
        update_forward_forecasts(candidates, markets_by_ticker, settings)
        closed = close_positions(portfolio, markets_by_ticker, histories, candidates_by_ticker, settings)
        opened = open_positions(portfolio, candidates, settings) if setting_bool(settings, "CRYPTO_PERPS_SHADOW_EXECUTION_ENABLED", False) else []
        write_json(PORTFOLIO_FILE, portfolio)
        report = summarize(portfolio, markets, candidates, opened, closed, settings)
        log(f"PERPS SHADOW SCAN markets={len(markets)} candidates={len(candidates)} eligible={report['eligible_count']} open={report['open_count']} settled={report['settled_count']} equity=${report['equity']:.2f}")
        event({"type": "perps_shadow_scan", "markets": len(markets), "candidates": len(candidates), "eligible": report["eligible_count"], "opened": len(opened), "closed": len(closed), "equity": report["equity"]})
        return report
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        log(f"PERPS SHADOW ERROR {message}")
        event({"type": "perps_shadow_error", "error": message})
        return summarize(portfolio, [], [], [], [], settings, error=message)
