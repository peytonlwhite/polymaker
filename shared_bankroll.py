import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from sports_audit_support import capper_parent_exposure_review


LOCAL_DATE_FORMAT = "%Y-%m-%d"
SETTINGS_FILE = Path("bot_settings.json")
CRYPTO_SETTINGS_FILE = Path("crypto_settings.json")
SPORTS_PORTFOLIO_FILE = Path("sports_paper_portfolio.json")
CRYPTO_PAPER_PORTFOLIO_FILE = Path("crypto_paper_portfolio.json")
CRYPTO_LIVE_PORTFOLIO_FILE = Path("crypto_live_portfolio.json")
# Retired ledgers remain read-only inputs to account risk; they cannot receive allocations.
RETIRED_LIVE_PORTFOLIOS = {"weather": Path("weather_live_portfolio.json")}
SPORTS_LIVE_RECONCILIATION_FILE = Path("sports_live_reconciliation.json")
CRYPTO_LIVE_RECONCILIATION_FILE = Path("crypto_live_reconciliation.json")
STATE_FILE = Path("shared_bankroll_state.json")
STATE_LOCK_FILE = Path("shared_bankroll_state.lock")
DAILY_RESET_FILE = Path("daily_pnl_reset.json")


_STATE_THREAD_LOCK = threading.RLock()
_STATE_LOCK_LOCAL = threading.local()


@contextmanager
def shared_state_lock(timeout_seconds=10.0):
    """Serialize shared-bankroll read/modify/write transactions across workers."""
    with _STATE_THREAD_LOCK:
        depth = int(getattr(_STATE_LOCK_LOCAL, "depth", 0))
        if depth:
            _STATE_LOCK_LOCAL.depth = depth + 1
            try:
                yield
            finally:
                _STATE_LOCK_LOCAL.depth -= 1
            return

        handle = open(STATE_LOCK_FILE, "a+b")
        acquired = False
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while not acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("shared_bankroll_state_lock_timeout")
                    time.sleep(0.02)
            _STATE_LOCK_LOCAL.depth = 1
            yield
        finally:
            _STATE_LOCK_LOCAL.depth = 0
            if acquired:
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


def serialized_shared_state(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        with shared_state_lock():
            return func(*args, **kwargs)
    return wrapper


DEFAULTS = {
    "SHARED_BANKROLL_ENABLED": "true",
    "SHARED_BANKROLL_RESERVATIONS_ENABLED": "true",
    "SHARED_RESERVATION_TTL_SECONDS": "120",
    "SHARED_SYSTEM_DAILY_TARGET_PCT": "0.034",
    "SHARED_SYSTEM_DAILY_TARGET_MIN": "20",
    "SHARED_SYSTEM_DAILY_TARGET_MAX": "50",
    "SHARED_CYCLE_COORDINATION_ENABLED": "true",
    "SHARED_CYCLE_MAX_CYCLES": "4",
    "SHARED_CYCLE_DECAY": "0.50",
    "SHARED_CYCLE_MIN_TARGET": "0.25",
    "SHARED_CYCLE_SINGLE_OWNER_ENABLED": "true",
    "SHARED_SYSTEM_DAILY_LOSS_PCT": "0",
    "SHARED_SYSTEM_DAILY_LOSS_CAP": "0",
    "SHARED_SYSTEM_MAX_OPEN_EXPOSURE_PCT": "0",
    "SHARED_SYSTEM_MAX_OPEN_EXPOSURE_CAP": "0",
    "SHARED_SPORTS_MAX_OPEN_EXPOSURE_PCT": "0",
    "SHARED_SPORTS_MAX_OPEN_EXPOSURE_CAP": "0",
    "SHARED_SPORTS_MAX_STAKE_PCT": "0",
    "SHARED_SPORTS_MAX_STAKE_CAP": "0",
    "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_PCT": "0",
    "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_CAP": "9999",
    "SHARED_CRYPTO_MAX_STAKE_PCT": "0",
    "SHARED_CRYPTO_MAX_STAKE_CAP": "9999",
    "SHARED_CRYPTO_ISOLATED_RISK_ENABLED": "true",
    "SHARED_CRYPTO_DAILY_LOSS_PCT": "0",
    "SHARED_CRYPTO_DAILY_LOSS_CAP": "0",
    "SHARED_CRYPTO_RISK_CAP_BUFFER": "0",
    "SHARED_PAUSE_CRYPTO_ON_SYSTEM_TARGET": "false",
    "SHARED_ALLOW_CRYPTO_PHASE_TWO_ON_SYSTEM_TARGET": "true",
    "SHARED_PAUSE_SPORTS_ON_SYSTEM_TARGET": "false",
    "SHARED_CRYPTO_LOSSES_TRIGGER_SPORTS_RECOVERY": "false",
    "SHARED_COUNT_CRYPTO_LIVE_PNL_IN_SYSTEM_TARGET": "true",
    "SHARED_RECOVERY_ENABLED": "true",
    "SHARED_RECOVERY_SINGLE_OWNER_ENABLED": "false",
    "SHARED_RECOVERY_WAITS_FOR_PHASE_TWO_ENABLED": "false",
    "SHARED_RECOVERY_INCLUDE_CRYPTO_PAPER": "true",
    "SHARED_RECOVERY_MIN_DRAWDOWN": "10",
    "SHARED_RECOVERY_MIN_DRAWDOWN_PCT": "0.02",
    "SHARED_RECOVERY_TARGET_PROFIT_MULTIPLIER": "0.40",
    "SHARED_RECOVERY_MAX_STAKE_PCT": "0.03",
    "SHARED_RECOVERY_MAX_STAKE_CAP": "20",
    "SHARED_RECOVERY_MAX_OPEN_SLICES": "3",
    "SHARED_RECOVERY_MAX_CLUSTER_OPEN": "1",
    "SHARED_RECOVERY_MAX_TOTAL_STAKE_PCT": "0.12",
    "SHARED_RECOVERY_MAX_SLICE_STAKE_PCT": "0.04",
    "SHARED_RECOVERY_QUALIFIED_ALLOCATION": "0.40",
    "SHARED_RECOVERY_STRONG_ALLOCATION": "0.70",
    "SHARED_RECOVERY_ELITE_ALLOCATION": "1.00",
    "SHARED_RECOVERY_STRONG_QUALITY_SCORE": "60",
    "SHARED_RECOVERY_ELITE_QUALITY_SCORE": "85",
    "SHARED_RECOVERY_CRYPTO_ELITE_CONFIDENCE": "90",
    "SHARED_RECOVERY_CRYPTO_ELITE_EDGE": "20",
    "SHARED_RECOVERY_SPORTS_ELITE_CONFIDENCE": "92",
    "SHARED_RECOVERY_SPORTS_ELITE_EDGE": "12",
    "SHARED_RECOVERY_SPORTS_ELITE_PRO_SCORE": "120",
    "SHARED_RECOVERY_SPORTS_ELITE_FINAL_SCORE": "115",
    "SHARED_CORRELATION_GUARD_ENABLED": "true",
    "SHARED_CORRELATION_TRIM_ENHANCED_TO_BASE": "true",
}


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def write_json(path, payload):
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
    raise last_error


def setting_bool(settings, key):
    return str(settings.get(key, DEFAULTS.get(key, ""))).lower() == "true"


def setting_float(settings, key):
    try:
        return float(settings.get(key, DEFAULTS.get(key, "0")) or 0)
    except (TypeError, ValueError):
        return float(DEFAULTS.get(key, "0") or 0)


def money(value):
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def is_today(value):
    parsed = parse_time(value)
    return bool(parsed and parsed.date() == datetime.now().astimezone().date())


def active_daily_reset_cutoff(strategy=None):
    reset = read_json(DAILY_RESET_FILE, {})
    today = datetime.now().astimezone().date().isoformat()
    if reset.get("local_date") != today:
        return None
    strategies = {
        str(value).strip().lower()
        for value in (reset.get("strategies") or [])
        if str(value).strip()
    }
    if strategy and strategies and str(strategy).strip().lower() not in strategies:
        return None
    return parse_time(reset.get("reset_at"))


def load_settings():
    settings = dict(DEFAULTS)
    settings.update({key: str(value) for key, value in read_json(SETTINGS_FILE, {}).items()})
    crypto_settings = read_json(CRYPTO_SETTINGS_FILE, {})
    for key, value in crypto_settings.items():
        if str(key).startswith("SHARED_") or str(key).startswith("CRYPTO_"):
            settings[key] = str(value)
    settings.update({key: value for key, value in os.environ.items() if key.startswith("SHARED_")})
    return settings


def reconciliation_cash_snapshot(path, source):
    recon = read_json(path, {})
    account = recon.get("account") or {}
    cash = account.get("cash_balance")
    if cash is None:
        cash = recon.get("cash_balance")
    if cash is None:
        cash = recon.get("local_balance_synced_to_remote_cash")
    if cash is None:
        return None
    balance_raw = account.get("balance_raw") or {}
    portfolio_value = account.get("portfolio_value_dollars")
    if portfolio_value is None:
        portfolio_value = balance_raw.get("portfolio_value_dollars")
    if portfolio_value is None and balance_raw.get("portfolio_value") is not None:
        # Kalshi's balance endpoint reports this field in cents.
        portfolio_value = money(balance_raw.get("portfolio_value")) / 100.0
    portfolio_value = money(portfolio_value)
    cash = money(cash)
    stamp = parse_time(recon.get("generated_at")) or parse_time(account.get("synced_at")) or datetime(1970, 1, 1).astimezone()
    return {
        "source": source,
        "cash": cash,
        "portfolio_value": portfolio_value,
        "equity": money(cash + portfolio_value),
        "generated_at": recon.get("generated_at") or account.get("synced_at"),
        "timestamp": stamp,
    }


def live_cash_from_reconciliation(return_snapshot=False):
    snapshots = [
        row for row in (
            reconciliation_cash_snapshot(SPORTS_LIVE_RECONCILIATION_FILE, "sports"),
            reconciliation_cash_snapshot(CRYPTO_LIVE_RECONCILIATION_FILE, "crypto"),
        )
        if row
    ]
    if not snapshots:
        return {} if return_snapshot else 0.0
    freshest = max(snapshots, key=lambda row: row["timestamp"])
    result = {key: value for key, value in freshest.items() if key != "timestamp"}
    return result if return_snapshot else result["cash"]


def open_rows(portfolio, strategy=None, live_only=False):
    rows = []
    for bet in portfolio.get("bets", []):
        if bet.get("status") != "open":
            continue
        if live_only and bet.get("mode") != "live":
            continue
        row = dict(bet)
        if strategy:
            row["strategy"] = strategy
        rows.append(row)
    return rows


def is_user_managed_row(row):
    return bool(
        row.get("strategy_owner") == "user_bet"
        or row.get("source") == "user_manual"
        or row.get("user_bet")
        or row.get("manual_bet")
    )


def paper_rows(portfolio, strategy=None):
    rows = []
    for bet in open_rows(portfolio, strategy=strategy, live_only=False):
        if bet.get("mode") == "live":
            continue
        rows.append(bet)
    return rows


def settled_rows_today(portfolio, strategy=None, live_only=True):
    sources = []
    sources.extend(portfolio.get("history", []))
    sources.extend([bet for bet in portfolio.get("bets", []) if bet.get("status") == "settled"])
    rows = []
    reset_cutoff = active_daily_reset_cutoff(strategy)
    for bet in sources:
        if live_only and bet.get("mode") != "live":
            continue
        if strategy in {"sports", "crypto"} and is_user_managed_row(bet):
            continue
        stamp = bet.get("settled_at") or bet.get("closed_at") or bet.get("placed_at")
        if not is_today(stamp):
            continue
        parsed = parse_time(stamp)
        if reset_cutoff and parsed and parsed <= reset_cutoff:
            continue
        row = dict(bet)
        if strategy:
            row["strategy"] = strategy
        rows.append(row)
    return rows


def exposure(rows):
    return money(sum(money(row.get("stake")) for row in rows))


def realized_profit(rows):
    return money(sum(money(row.get("profit")) for row in rows))


def effective_cap(cash, pct, flat_cap):
    pct_cap = money(cash * pct) if pct > 0 else 0.0
    caps = [value for value in (pct_cap, flat_cap) if value > 0]
    return money(min(caps)) if caps else 0.0


def effective_cap_with_buffer(cash, pct, flat_cap, buffer=0.0):
    """Add an explicit dollar buffer after the percentage/flat cap resolves."""
    base = effective_cap(cash, pct, flat_cap)
    extra = money(max(0.0, float(buffer or 0)))
    return money(base + extra) if base > 0 or extra > 0 else 0.0


def shared_loss_room_snapshot(state, strategy=None, settings=None):
    settings = settings or (state.get("settings") or {})
    crypto_isolated = strategy == "crypto" and setting_bool(settings, "SHARED_CRYPTO_ISOLATED_RISK_ENABLED")
    if crypto_isolated:
        cap = money(state.get("crypto_live_daily_loss_cap"))
        realized_loss = money(state.get("crypto_live_daily_loss"))
        open_exposure = money(
            state.get("crypto_bot_live_open_exposure", state.get("crypto_live_open_exposure"))
            + (state.get("reserved_by_strategy") or {}).get("crypto", 0)
        )
        scope = "crypto"
        policy = "crypto_bot_realized_daily_loss_plus_crypto_bot_open_exposure"
    else:
        cap = money(state.get("system_live_daily_loss_cap"))
        realized_loss = money(state.get("system_live_daily_loss"))
        open_exposure = money(state.get("system_live_open_exposure"))
        scope = "system"
        policy = "realized_daily_loss_plus_all_live_open_exposure"
    worst_case_used = money(realized_loss + open_exposure)
    return {
        "scope": scope,
        "cap": cap,
        "realized_daily_loss": realized_loss,
        "open_exposure": open_exposure,
        "worst_case_used": worst_case_used,
        "remaining": money(max(0.0, cap - worst_case_used)) if cap > 0 else None,
        "sports_open_exposure": money(state.get("sports_live_open_exposure")),
        "retired_open_exposure": money(state.get("retired_live_open_exposure")),
        "crypto_open_exposure": money(state.get("crypto_live_open_exposure")),
        "crypto_bot_open_exposure": money(state.get("crypto_bot_live_open_exposure", state.get("crypto_live_open_exposure"))),
        "crypto_user_open_exposure": money(state.get("crypto_user_live_open_exposure")),
        "reserved_exposure": money(state.get("reserved_total")),
        "other_strategy_exposure_ignored": crypto_isolated,
        "user_managed_exposure_ignored": crypto_isolated,
        "policy": policy,
    }


def daily_target(cash, settings):
    target = max(setting_float(settings, "SHARED_SYSTEM_DAILY_TARGET_MIN"), cash * setting_float(settings, "SHARED_SYSTEM_DAILY_TARGET_PCT"))
    max_target = setting_float(settings, "SHARED_SYSTEM_DAILY_TARGET_MAX")
    if max_target > 0:
        target = min(target, max_target)
    return money(target)


def shared_cycle_plan(realized_profit, base_target, settings):
    """Build one system-wide profit ladder without increasing a target after a loss."""
    enabled = setting_bool(settings, "SHARED_CYCLE_COORDINATION_ENABLED")
    max_cycles = max(1, int(setting_float(settings, "SHARED_CYCLE_MAX_CYCLES") or 1))
    decay = min(1.0, max(0.01, setting_float(settings, "SHARED_CYCLE_DECAY") or 0.5))
    min_target = max(0.01, setting_float(settings, "SHARED_CYCLE_MIN_TARGET") or 0.25)
    profit = money(realized_profit)
    positive_profit = max(0.0, profit)
    cycles = []
    cumulative_target = 0.0
    completed_cycles = 0
    for cycle_number in range(1, max_cycles + 1):
        target = money(max(min_target, float(base_target or 0) * (decay ** (cycle_number - 1))))
        prior_cumulative = cumulative_target
        cumulative_target = money(cumulative_target + target)
        contribution = money(max(0.0, positive_profit - prior_cumulative))
        complete = positive_profit >= cumulative_target
        if complete:
            completed_cycles = cycle_number
        cycles.append({
            "cycle": cycle_number,
            "target": target,
            "cumulative_target": cumulative_target,
            "contribution": money(min(target, contribution)),
            "target_remaining": 0.0 if complete else money(max(0.0, target - contribution)),
            "complete": complete,
        })
    current_cycle = min(max_cycles + 1, completed_cycles + 1)
    current = cycles[min(current_cycle, max_cycles) - 1]
    return {
        "enabled": enabled,
        "status": "complete" if current_cycle > max_cycles else "active",
        "base_target": money(base_target),
        "realized_profit": profit,
        "positive_profit": money(positive_profit),
        "current_cycle": current_cycle,
        "max_cycles": max_cycles,
        "completed_cycles": completed_cycles,
        "cycle_decay": decay,
        "min_cycle_target": money(min_target),
        "target": money(current.get("target")),
        "target_remaining": 0.0 if current_cycle > max_cycles else money(current.get("target_remaining")),
        "cumulative_target": money(current.get("cumulative_target")),
        "cycles": cycles,
    }


def coordination_owner(rows_by_strategy, reservations, owner_kind, settings=None):
    """Return the strategy already carrying a cycle or recovery position."""
    settings = settings or load_settings()
    owners = set()
    for strategy, rows in rows_by_strategy.items():
        for row in rows:
            if strategy == "crypto" and owner_kind == "phase_two":
                minimum_price = setting_float(settings, "CRYPTO_ANALYTICS_MIN_ENTRY_PRICE_CENTS")
                if minimum_price > 0 and money(row.get("entry_price")) < minimum_price:
                    continue
            strategy_owner = str(row.get("strategy_owner") or "")
            phase_active = bool((row.get("phase_two") or {}).get("active") or row.get("phase_two_attempt_id"))
            recovery_active = bool(
                (row.get("recovery_staking") or {}).get("active")
                or (row.get("recovery_basket") or {}).get("active")
                or (row.get("shared_recovery") or {}).get("active")
            )
            if owner_kind == "phase_two" and (strategy_owner == "phase_two_cycle" or phase_active):
                owners.add(strategy)
            if owner_kind == "recovery" and (strategy_owner in {"recovery", "recovery_basket", "shared_recovery"} or recovery_active):
                owners.add(strategy)
    for reservation in reservations:
        metadata = reservation.get("metadata") or {}
        strategy_owner = str(metadata.get("strategy_owner") or metadata.get("owner") or "")
        if owner_kind == "phase_two" and (strategy_owner == "phase_two_cycle" or metadata.get("phase_two")):
            owners.add(str(reservation.get("strategy") or "unknown"))
        if owner_kind == "recovery" and (strategy_owner in {"recovery", "recovery_basket", "shared_recovery"} or metadata.get("recovery")):
            owners.add(str(reservation.get("strategy") or "unknown"))
    if len(owners) > 1:
        return "mixed"
    return next(iter(owners), None)


def row_metadata(row):
    """Merge a stored bet or reservation wrapper into one metadata view."""
    merged = dict(row or {})
    merged.update((row or {}).get("metadata") or {})
    return merged


def correlation_cluster_key(strategy, row):
    """Identify the underlying event risk; different markets in one event share a cluster."""
    data = row_metadata(row)
    strategy = str(strategy or data.get("strategy") or "unknown").lower()
    if strategy == "sports":
        raw = data.get("game_key") or data.get("event_key") or data.get("event_ticker")
        if not raw:
            raw = data.get("matchup") or data.get("game")
    elif strategy == "crypto":
        raw = data.get("event_ticker") or data.get("event_key")
        if not raw:
            raw = data.get("ticker")
    else:
        raw = data.get("event_ticker") or data.get("game_key") or data.get("event_key") or data.get("ticker")
    if not raw:
        return None
    return f"{strategy}:{str(raw).strip().lower()}"


def is_recovery_row(row):
    data = row_metadata(row)
    owner = str(data.get("strategy_owner") or data.get("owner") or "")
    return bool(
        owner in {"recovery", "recovery_basket", "shared_recovery"}
        or data.get("recovery")
        or (data.get("recovery_staking") or {}).get("active")
        or (data.get("recovery_basket") or {}).get("active")
        or (data.get("shared_recovery") or {}).get("active")
    )


def expected_position_profit(row):
    data = row_metadata(row)
    explicit = data.get("recovery_expected_profit") or data.get("expected_profit")
    if explicit is not None:
        return money(explicit)
    stake = money(data.get("stake"))
    price = money(data.get("entry_price") or data.get("price_cents"))
    if stake > 0 and 0 < price < 100:
        return money(stake * ((100.0 / price) - 1.0))
    return 0.0


def portfolio_coordination_snapshot(rows_by_strategy, reservations, settings):
    clusters = {}
    recovery_rows = []

    def add(strategy, row, source):
        data = row_metadata(row)
        cluster = correlation_cluster_key(strategy, data)
        if cluster:
            summary = clusters.setdefault(cluster, {
                "cluster": cluster,
                "strategies": [],
                "positions": 0,
                "stake": 0.0,
                "phase_two_positions": 0,
                "recovery_positions": 0,
            })
            if strategy not in summary["strategies"]:
                summary["strategies"].append(strategy)
            summary["positions"] += 1
            summary["stake"] = money(summary["stake"] + money(data.get("stake")))
            owner = str(data.get("strategy_owner") or data.get("owner") or "")
            if owner == "phase_two_cycle" or data.get("phase_two") or data.get("phase_two_attempt_id"):
                summary["phase_two_positions"] += 1
            if is_recovery_row(data):
                summary["recovery_positions"] += 1
        if is_recovery_row(data):
            recovery_rows.append({
                "strategy": strategy,
                "cluster": cluster,
                "stake": money(data.get("stake")),
                "expected_profit": expected_position_profit(data),
                "source": source,
            })

    for strategy, rows in rows_by_strategy.items():
        for row in rows:
            add(strategy, row, "position")
    for reservation in reservations:
        add(str(reservation.get("strategy") or "unknown"), reservation, "reservation")

    recovery_clusters = sorted({row["cluster"] for row in recovery_rows if row.get("cluster")})
    return {
        "correlation": {
            "enabled": setting_bool(settings, "SHARED_CORRELATION_GUARD_ENABLED"),
            "cluster_count": len(clusters),
            "clusters": clusters,
        },
        "recovery": {
            "max_open_slices": max(1, int(setting_float(settings, "SHARED_RECOVERY_MAX_OPEN_SLICES") or 3)),
            "max_cluster_open": max(1, int(setting_float(settings, "SHARED_RECOVERY_MAX_CLUSTER_OPEN") or 1)),
            "open_slices": len(recovery_rows),
            "open_stake": money(sum(row["stake"] for row in recovery_rows)),
            "open_expected_profit": money(sum(row["expected_profit"] for row in recovery_rows)),
            "clusters": recovery_clusters,
            "slices": recovery_rows,
        },
    }


def prune_reservations(state, now=None):
    now = now or datetime.now().astimezone()
    active = []
    expired = []
    for reservation in state.get("reservations", []):
        expires = parse_time(reservation.get("expires_at"))
        if reservation.get("status") == "active" and expires and expires > now:
            active.append(reservation)
        elif reservation.get("status") == "active":
            reservation["status"] = "expired"
            expired.append(reservation)
    state["reservations"] = active + state.get("reservation_history", [])[-200:] + expired[-50:]
    state["reservation_history"] = [r for r in state["reservations"] if r.get("status") != "active"][-250:]
    state["reservations"] = active
    return active


@serialized_shared_state
def build_shared_bankroll_state(settings=None, save=True):
    settings = settings or load_settings()
    sports_portfolio = read_json(SPORTS_PORTFOLIO_FILE, {"bets": [], "history": []})
    crypto_paper_portfolio = read_json(CRYPTO_PAPER_PORTFOLIO_FILE, {"bets": []})
    crypto_live_portfolio = read_json(CRYPTO_LIVE_PORTFOLIO_FILE, {"bets": []})
    retired_portfolios = {
        strategy: read_json(path, {"bets": [], "history": []})
        for strategy, path in RETIRED_LIVE_PORTFOLIOS.items()
    }
    stored = read_json(STATE_FILE, {"reservations": [], "reservation_history": []})
    active_reservations = prune_reservations(stored)

    cash_snapshot = live_cash_from_reconciliation(return_snapshot=True)
    cash = money(cash_snapshot.get("cash"))
    portfolio_value = money(cash_snapshot.get("portfolio_value"))
    account_equity = money(
        cash_snapshot.get("equity") or cash + portfolio_value
    )
    sports_open_live = open_rows(sports_portfolio, "sports", live_only=True)
    crypto_open_live = open_rows(crypto_live_portfolio, "crypto", live_only=True)
    retired_open_live = [
        row for strategy, portfolio in retired_portfolios.items()
        for row in open_rows(portfolio, strategy, live_only=True)
    ]
    crypto_bot_open_live = [row for row in crypto_open_live if not is_user_managed_row(row)]
    crypto_user_open_live = [row for row in crypto_open_live if is_user_managed_row(row)]
    crypto_open_paper = paper_rows(crypto_paper_portfolio, "crypto_paper")
    sports_today_live = settled_rows_today(sports_portfolio, "sports", live_only=True)
    crypto_today_live = settled_rows_today(crypto_live_portfolio, "crypto", live_only=True)
    retired_today_live = [
        row for strategy, portfolio in retired_portfolios.items()
        for row in settled_rows_today(portfolio, strategy, live_only=True)
    ]
    crypto_today_all_live = settled_rows_today(crypto_live_portfolio, None, live_only=True)
    crypto_user_today_live = [row for row in crypto_today_all_live if is_user_managed_row(row)]
    crypto_today_all = settled_rows_today(crypto_paper_portfolio, "crypto_paper", live_only=False)

    reserved_by_strategy = {}
    reserved_by_scope = {}
    for reservation in active_reservations:
        reserved_by_strategy[reservation.get("strategy", "unknown")] = money(
            reserved_by_strategy.get(reservation.get("strategy", "unknown"), 0) + money(reservation.get("stake"))
        )
        scope = str(
            (reservation.get("metadata") or {}).get("reservation_scope")
            or reservation.get("strategy")
            or "unknown"
        )
        reserved_by_scope[scope] = money(
            reserved_by_scope.get(scope, 0) + money(reservation.get("stake"))
        )
    reserved_total = money(sum(reserved_by_strategy.values()))
    sports_live_exposure = exposure(sports_open_live)
    crypto_live_exposure = exposure(crypto_open_live)
    crypto_bot_live_exposure = exposure(crypto_bot_open_live)
    crypto_user_live_exposure = exposure(crypto_user_open_live)
    retired_live_exposure = exposure(retired_open_live)
    system_live_exposure = money(sports_live_exposure + crypto_live_exposure + retired_live_exposure + reserved_total)
    system_live_profit = money(realized_profit(sports_today_live) + realized_profit(crypto_today_live) + realized_profit(retired_today_live))
    target = daily_target(cash, settings)
    cycle_plan = shared_cycle_plan(system_live_profit, target, settings)
    rows_by_strategy = {"sports": sports_open_live, "crypto": crypto_bot_open_live, "retired": retired_open_live}
    phase_two_owner = coordination_owner(rows_by_strategy, active_reservations, "phase_two", settings=settings)
    recovery_owner = coordination_owner(rows_by_strategy, active_reservations, "recovery", settings=settings)
    portfolio_snapshot = portfolio_coordination_snapshot(rows_by_strategy, active_reservations, settings)
    recovery_realized_basis = system_live_profit
    if setting_bool(settings, "SHARED_RECOVERY_INCLUDE_CRYPTO_PAPER"):
        recovery_realized_basis = money(recovery_realized_basis + realized_profit(crypto_today_all))
    recovery_drawdown = money(abs(min(0.0, recovery_realized_basis)))
    recovery_min_drawdown = money(max(
        setting_float(settings, "SHARED_RECOVERY_MIN_DRAWDOWN"),
        cash * setting_float(settings, "SHARED_RECOVERY_MIN_DRAWDOWN_PCT"),
    ))
    recovery_campaign_target = money(recovery_drawdown * setting_float(settings, "SHARED_RECOVERY_TARGET_PROFIT_MULTIPLIER"))
    recovery_target_remaining = money(max(0.0, recovery_campaign_target - portfolio_snapshot["recovery"]["open_expected_profit"]))
    recovery_slots_remaining = max(0, portfolio_snapshot["recovery"]["max_open_slices"] - portfolio_snapshot["recovery"]["open_slices"])
    recovery_slice_target = money(recovery_target_remaining / recovery_slots_remaining) if recovery_slots_remaining else 0.0
    recovery_armed = bool(
        setting_bool(settings, "SHARED_RECOVERY_ENABLED")
        and recovery_drawdown >= recovery_min_drawdown
        and recovery_target_remaining > 0
        and recovery_slots_remaining > 0
    )
    loss_cap = effective_cap(cash, setting_float(settings, "SHARED_SYSTEM_DAILY_LOSS_PCT"), setting_float(settings, "SHARED_SYSTEM_DAILY_LOSS_CAP"))
    crypto_risk_cap_buffer = setting_float(
        settings,
        "SHARED_CRYPTO_RISK_CAP_BUFFER",
    )
    crypto_loss_cap = effective_cap_with_buffer(
        cash,
        setting_float(settings, "SHARED_CRYPTO_DAILY_LOSS_PCT"),
        setting_float(settings, "SHARED_CRYPTO_DAILY_LOSS_CAP"),
        crypto_risk_cap_buffer,
    )
    system_exposure_cap = effective_cap(cash, setting_float(settings, "SHARED_SYSTEM_MAX_OPEN_EXPOSURE_PCT"), setting_float(settings, "SHARED_SYSTEM_MAX_OPEN_EXPOSURE_CAP"))
    crypto_exposure_cap = effective_cap_with_buffer(
        cash,
        setting_float(settings, "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_PCT"),
        setting_float(settings, "SHARED_CRYPTO_MAX_OPEN_EXPOSURE_CAP"),
        crypto_risk_cap_buffer,
    )
    crypto_live_profit = realized_profit(crypto_today_live)
    crypto_live_daily_loss = money(abs(min(0.0, crypto_live_profit)))
    crypto_risk_open_exposure = money(crypto_bot_live_exposure + reserved_by_strategy.get("crypto", 0))

    state = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "enabled": setting_bool(settings, "SHARED_BANKROLL_ENABLED"),
        "cash": cash,
        "portfolio_value": portfolio_value,
        "account_equity": account_equity,
        "cash_source": cash_snapshot.get("source"),
        "cash_source_generated_at": cash_snapshot.get("generated_at"),
        "daily_target": target,
        "daily_pnl_reset_at": read_json(DAILY_RESET_FILE, {}).get("reset_at") if active_daily_reset_cutoff() else None,
        "system_live_realized_profit": system_live_profit,
        "system_live_target_remaining": money(max(0.0, target - system_live_profit)),
        "cycle_coordination": {
            **cycle_plan,
            "single_owner_enabled": setting_bool(settings, "SHARED_CYCLE_SINGLE_OWNER_ENABLED"),
            "owner": phase_two_owner,
        },
        "recovery_coordination": {
            "single_owner_enabled": setting_bool(settings, "SHARED_RECOVERY_SINGLE_OWNER_ENABLED"),
            "waits_for_phase_two": setting_bool(settings, "SHARED_RECOVERY_WAITS_FOR_PHASE_TWO_ENABLED"),
            "owner": recovery_owner,
            "phase_two_owner": phase_two_owner,
            **portfolio_snapshot["recovery"],
            "armed": recovery_armed,
            "realized_profit_basis": recovery_realized_basis,
            "drawdown": recovery_drawdown,
            "effective_min_drawdown": recovery_min_drawdown,
            "campaign_target_profit": recovery_campaign_target,
            "target_profit_remaining": recovery_target_remaining,
            "slots_remaining": recovery_slots_remaining,
            "slice_target_profit": recovery_slice_target,
        },
        "correlation_coordination": portfolio_snapshot["correlation"],
        "system_live_daily_loss": money(abs(min(0.0, system_live_profit))),
        "system_live_daily_loss_cap": loss_cap,
        "system_worst_case_loss_used": money(
            abs(min(0.0, system_live_profit)) + system_live_exposure
        ),
        "system_worst_case_loss_remaining": (
            money(max(
                0.0,
                loss_cap
                - abs(min(0.0, system_live_profit))
                - system_live_exposure,
            ))
            if loss_cap > 0
            else None
        ),
        "system_live_open_exposure": system_live_exposure,
        "system_live_open_exposure_cap": system_exposure_cap,
        "sports_live_open_exposure": sports_live_exposure,
        "retired_live_open_exposure": retired_live_exposure,
        "retired_live_open_count": len(retired_open_live),
        "retired_live_today_profit": realized_profit(retired_today_live),
        "crypto_live_open_exposure": crypto_live_exposure,
        "crypto_bot_live_open_exposure": crypto_bot_live_exposure,
        "crypto_user_live_open_exposure": crypto_user_live_exposure,
        "crypto_isolated_risk_enabled": setting_bool(settings, "SHARED_CRYPTO_ISOLATED_RISK_ENABLED"),
        "crypto_live_daily_loss": crypto_live_daily_loss,
        "crypto_live_daily_loss_cap": crypto_loss_cap,
        "crypto_risk_cap_buffer": money(crypto_risk_cap_buffer),
        "crypto_worst_case_loss_used": money(crypto_live_daily_loss + crypto_risk_open_exposure),
        "crypto_worst_case_loss_remaining": (
            money(max(0.0, crypto_loss_cap - crypto_live_daily_loss - crypto_risk_open_exposure))
            if crypto_loss_cap > 0
            else None
        ),
        "crypto_paper_open_exposure": exposure(crypto_open_paper),
        "sports_live_today_profit": realized_profit(sports_today_live),
        "crypto_live_today_profit": crypto_live_profit,
        "crypto_user_live_today_profit": realized_profit(crypto_user_today_live),
        "crypto_paper_today_profit": realized_profit(crypto_today_all),
        "reservations_enabled": setting_bool(settings, "SHARED_BANKROLL_RESERVATIONS_ENABLED"),
        "reserved_total": reserved_total,
        "reserved_by_strategy": reserved_by_strategy,
        "reserved_by_scope": reserved_by_scope,
        "reservations": active_reservations,
        "active_reservations": active_reservations,
        "capper_parent_commitments": stored.get("capper_parent_commitments") or {},
        "settings": {
            key: settings.get(key)
            for key in sorted(DEFAULTS)
            if key.startswith("SHARED_")
        },
        "strategy_caps": {
            "sports": {
                "max_open_exposure": effective_cap(cash, setting_float(settings, "SHARED_SPORTS_MAX_OPEN_EXPOSURE_PCT"), setting_float(settings, "SHARED_SPORTS_MAX_OPEN_EXPOSURE_CAP")),
                "max_stake": effective_cap(cash, setting_float(settings, "SHARED_SPORTS_MAX_STAKE_PCT"), setting_float(settings, "SHARED_SPORTS_MAX_STAKE_CAP")),
            },
            "crypto": {
                "max_open_exposure": crypto_exposure_cap,
                "max_stake": effective_cap(cash, setting_float(settings, "SHARED_CRYPTO_MAX_STAKE_PCT"), setting_float(settings, "SHARED_CRYPTO_MAX_STAKE_CAP")),
            },
        },
    }
    if save:
        write_json(STATE_FILE, {**state, "reservation_history": stored.get("reservation_history", [])[-250:]})
    return state


def live_order_review(strategy, stake, price_cents=None, settings=None, metadata=None):
    if strategy not in {"sports", "crypto"}:
        return {"ok": False, "error": "shared_unsupported_strategy", "approved_stake": 0.0, "state": {}}
    settings = settings or load_settings()
    metadata = metadata or {}
    state = build_shared_bankroll_state(settings, save=True)
    if not state.get("enabled"):
        return {"ok": True, "enabled": False, "approved_stake": money(stake), "state": state}
    strategy = str(strategy or "unknown")
    crypto_isolated = strategy == "crypto" and setting_bool(settings, "SHARED_CRYPTO_ISOLATED_RISK_ENABLED")
    requested = money(stake)
    if requested <= 0:
        return {"ok": False, "error": "shared_invalid_stake", "approved_stake": 0.0, "state": state}
    if state["cash"] <= 0:
        return {"ok": False, "error": "shared_no_live_cash", "approved_stake": 0.0, "state": state}
    owner = str(metadata.get("strategy_owner") or metadata.get("owner") or "")
    is_live_campaign = (
        (strategy == "sports" and owner == "live_campaign" and bool(metadata.get("live_campaign")))
        or (
            strategy == "crypto"
            and owner == "crypto_15m_campaign"
            and bool(metadata.get("crypto_live_campaign"))
        )
    )
    if metadata.get("liquidity_only"):
        available_cash = money(
            max(0.0, state["cash"] - state.get("reserved_total", 0))
        )
        approved = money(min(requested, available_cash))
        min_contract = money(float(price_cents) / 100.0) if price_cents else 0.0
        if approved + 1e-9 < min_contract or approved <= 0:
            return {
                "ok": False,
                "enabled": True,
                "error": "shared_cash_below_one_contract",
                "requested_stake": requested,
                "approved_stake": approved,
                "min_contract_stake": min_contract,
                "available_cash": available_cash,
                "state": state,
            }
        return {
            "ok": True,
            "enabled": True,
            "strategy": strategy,
            "requested_stake": requested,
            "approved_stake": approved,
            "liquidity_only": True,
            "available_cash": available_cash,
            "state": state,
        }
    loss_room = shared_loss_room_snapshot(state, strategy=strategy, settings=settings)
    daily_loss_remaining = loss_room["remaining"] if loss_room["cap"] > 0 else requested
    if loss_room["cap"] > 0 and daily_loss_remaining <= 0:
        return {
            "ok": False,
            "error": "shared_worst_case_loss_room_exhausted",
            "legacy_error": "shared_daily_loss_cap_hit",
            "approved_stake": 0.0,
            "loss_room": loss_room,
            "state": state,
        }
    target_hit = state["daily_target"] > 0 and state["system_live_realized_profit"] >= state["daily_target"]
    is_phase_two = owner == "phase_two_cycle" or bool(metadata.get("phase_two"))
    is_recovery = owner in {"recovery", "recovery_basket", "shared_recovery"} or bool(metadata.get("recovery"))
    cycle_coordination = state.get("cycle_coordination") or {}
    recovery_coordination = state.get("recovery_coordination") or {}
    if cycle_coordination.get("enabled") and is_phase_two:
        if cycle_coordination.get("status") == "complete":
            return {"ok": False, "error": "shared_cycle_plan_complete", "approved_stake": 0.0, "state": state}
        active_owner = cycle_coordination.get("owner")
        if cycle_coordination.get("single_owner_enabled") and active_owner and active_owner != strategy:
            return {"ok": False, "error": "shared_phase_two_owned_by_other_strategy", "approved_stake": 0.0, "state": state}
    if owner == "live_core" and cycle_coordination.get("enabled") and cycle_coordination.get("status") == "complete":
        return {"ok": False, "error": "shared_cycle_plan_complete", "approved_stake": 0.0, "state": state}
    if is_recovery:
        phase_owner = recovery_coordination.get("phase_two_owner")
        if recovery_coordination.get("waits_for_phase_two") and phase_owner:
            return {"ok": False, "error": "shared_phase_two_position_active", "approved_stake": 0.0, "state": state}
        cluster = correlation_cluster_key(strategy, metadata)
        if not cluster:
            return {"ok": False, "error": "shared_recovery_cluster_missing", "approved_stake": 0.0, "state": state}
        if recovery_coordination.get("open_slices", 0) >= recovery_coordination.get("max_open_slices", 3):
            return {"ok": False, "error": "shared_recovery_campaign_full", "approved_stake": 0.0, "state": state}
        existing_cluster = ((state.get("correlation_coordination") or {}).get("clusters") or {}).get(cluster)
        if existing_cluster and existing_cluster.get("positions", 0) >= recovery_coordination.get("max_cluster_open", 1):
            return {"ok": False, "error": "shared_recovery_cluster_already_open", "approved_stake": 0.0, "state": state}
    if target_hit:
        if strategy == "crypto" and setting_bool(settings, "SHARED_PAUSE_CRYPTO_ON_SYSTEM_TARGET"):
            allowed_owner = owner in {"phase_two_cycle", "small_edge"}
            if not (allowed_owner and setting_bool(settings, "SHARED_ALLOW_CRYPTO_PHASE_TWO_ON_SYSTEM_TARGET")):
                return {"ok": False, "error": "shared_system_target_hit_crypto_paused", "approved_stake": 0.0, "state": state}
        if strategy == "sports" and setting_bool(settings, "SHARED_PAUSE_SPORTS_ON_SYSTEM_TARGET"):
            return {"ok": False, "error": "shared_system_target_hit_sports_paused", "approved_stake": 0.0, "state": state}

    if is_live_campaign:
        available_cash = money(max(0.0, state["cash"] - state.get("reserved_total", 0)))
        caps = state["strategy_caps"].get(strategy, {})
        strategy_open_key = "crypto_bot_live_open_exposure" if crypto_isolated else f"{strategy}_live_open_exposure"
        current_strategy_exposure = money(
            state.get(strategy_open_key, state.get(f"{strategy}_live_open_exposure", 0))
            + state.get("reserved_by_strategy", {}).get(strategy, 0)
        )
        remaining_system = (
            money(max(0.0, state["system_live_open_exposure_cap"] - state["system_live_open_exposure"]))
            if state["system_live_open_exposure_cap"] > 0 and not crypto_isolated
            else requested
        )
        remaining_strategy = (
            money(max(0.0, caps.get("max_open_exposure", 0) - current_strategy_exposure))
            if caps.get("max_open_exposure", 0) > 0
            else requested
        )
        configured_campaign_cap = (
            max(0.0, float(metadata.get("campaign_max_stake") or 0))
            if strategy == "sports"
            else requested
        )
        max_stake = caps.get("max_stake", 0) or requested
        approved = money(min(
            requested,
            configured_campaign_cap,
            available_cash,
            max_stake,
            remaining_system,
            remaining_strategy,
            daily_loss_remaining,
        ))
        if price_cents:
            min_contract = money(float(price_cents) / 100.0)
            if approved < min_contract:
                return {
                    "ok": False,
                    "error": "shared_caps_below_one_contract",
                    "approved_stake": approved,
                    "min_contract_stake": min_contract,
                    "state": state,
                }
        if approved <= 0:
            return {"ok": False, "error": "shared_no_live_cash", "approved_stake": 0.0, "state": state}
        return {
            "ok": True,
            "enabled": True,
            "strategy": strategy,
            "requested_stake": requested,
            "approved_stake": approved,
            "target_hit": target_hit,
            "strategy_owner": owner,
            "campaign_limits_owned_locally": True,
            "loss_room": loss_room,
            "state": state,
        }

    caps = state["strategy_caps"].get(strategy, {})
    strategy_open_key = "crypto_bot_live_open_exposure" if crypto_isolated else f"{strategy}_live_open_exposure"
    current_strategy_exposure = money(
        state.get(strategy_open_key, state.get(f"{strategy}_live_open_exposure", 0))
        + state.get("reserved_by_strategy", {}).get(strategy, 0)
    )
    remaining_system = (
        state["system_live_open_exposure_cap"] - state["system_live_open_exposure"]
        if state["system_live_open_exposure_cap"] > 0 and not crypto_isolated
        else requested
    )
    remaining_strategy = caps.get("max_open_exposure", 0) - current_strategy_exposure if caps.get("max_open_exposure", 0) > 0 else requested
    max_stake = caps.get("max_stake", 0) or requested
    available_cash = money(
        max(0.0, state["cash"] - state.get("reserved_total", 0))
    )
    if is_recovery:
        recovery_exposure_cap = money(state["cash"] * setting_float(settings, "SHARED_RECOVERY_MAX_TOTAL_STAKE_PCT"))
        if recovery_exposure_cap > 0:
            recovery_scope_exposure = current_strategy_exposure if crypto_isolated else state["system_live_open_exposure"]
            remaining_system = money(max(0.0, recovery_exposure_cap - recovery_scope_exposure))
            remaining_strategy = money(max(0.0, recovery_exposure_cap - current_strategy_exposure))
        recovery_stake_cap = effective_cap(
            state["cash"],
            setting_float(settings, "SHARED_RECOVERY_MAX_STAKE_PCT"),
            setting_float(settings, "SHARED_RECOVERY_MAX_STAKE_CAP"),
        )
        if recovery_stake_cap > 0:
            max_stake = recovery_stake_cap
    approved = money(min(
        requested,
        max_stake,
        remaining_system,
        remaining_strategy,
        daily_loss_remaining,
        available_cash,
    ))
    correlation_adjusted = False
    cluster = correlation_cluster_key(strategy, metadata)
    existing_cluster = ((state.get("correlation_coordination") or {}).get("clusters") or {}).get(cluster) if cluster else None
    if (
        setting_bool(settings, "SHARED_CORRELATION_GUARD_ENABLED")
        and setting_bool(settings, "SHARED_CORRELATION_TRIM_ENHANCED_TO_BASE")
        and metadata.get("enhanced_sizing")
        and existing_cluster
        and existing_cluster.get("positions", 0) > 0
        and not is_recovery
    ):
        base_stake = money(metadata.get("base_stake"))
        if base_stake > 0:
            approved = money(min(approved, base_stake))
            correlation_adjusted = approved < requested
    if is_recovery:
        max_total = money(state["cash"] * setting_float(settings, "SHARED_RECOVERY_MAX_TOTAL_STAKE_PCT"))
        if max_total > 0:
            approved = money(min(approved, max(0.0, max_total - money(recovery_coordination.get("open_stake")))))
    if price_cents:
        min_contract = money(float(price_cents) / 100.0)
        if approved < min_contract:
            return {
                "ok": False,
                "error": "shared_caps_below_one_contract",
                "approved_stake": approved,
                "min_contract_stake": min_contract,
                "state": state,
            }
    if approved <= 0:
        return {"ok": False, "error": "shared_exposure_cap_hit", "approved_stake": 0.0, "state": state}
    return {
        "ok": True,
        "enabled": True,
        "strategy": strategy,
        "requested_stake": requested,
        "approved_stake": approved,
        "target_hit": target_hit,
        "strategy_owner": owner,
        "correlation_cluster": cluster,
        "correlation_adjusted": correlation_adjusted,
        "loss_room": loss_room,
        "state": state,
    }


def shared_recovery_context(strategy="crypto", settings=None, cluster_key=None):
    settings = settings or load_settings()
    state = build_shared_bankroll_state(settings, save=True)
    context = {
        "enabled": setting_bool(settings, "SHARED_RECOVERY_ENABLED"),
        "strategy": strategy,
        "active": False,
        "reason": "disabled",
        "cash": state.get("cash", 0),
        "system_live_realized_profit": state.get("system_live_realized_profit", 0),
        "crypto_paper_today_profit": state.get("crypto_paper_today_profit", 0),
        "drawdown": 0.0,
        "effective_min_drawdown": 0.0,
        "target_profit": 0.0,
        "max_recovery_stake": 0.0,
        "state": state,
        "coordination_owner": (state.get("recovery_coordination") or {}).get("owner"),
        "phase_two_owner": (state.get("recovery_coordination") or {}).get("phase_two_owner"),
        "cluster": cluster_key,
    }
    if not context["enabled"]:
        return context
    recovery_coordination = state.get("recovery_coordination") or {}
    if recovery_coordination.get("waits_for_phase_two") and recovery_coordination.get("phase_two_owner"):
        context["reason"] = "shared_phase_two_position_active"
        return context
    if recovery_coordination.get("open_slices", 0) >= recovery_coordination.get("max_open_slices", 3):
        context["reason"] = "shared_recovery_campaign_full"
        return context
    if cluster_key:
        existing_cluster = ((state.get("correlation_coordination") or {}).get("clusters") or {}).get(cluster_key)
        if existing_cluster and existing_cluster.get("positions", 0) >= recovery_coordination.get("max_cluster_open", 1):
            context["reason"] = "shared_recovery_cluster_already_open"
            return context
    realized = money(state.get("system_live_realized_profit"))
    if setting_bool(settings, "SHARED_RECOVERY_INCLUDE_CRYPTO_PAPER"):
        realized = money(realized + money(state.get("crypto_paper_today_profit")))
    drawdown = money(abs(min(0.0, realized)))
    cash = money(state.get("cash"))
    min_drawdown = money(max(setting_float(settings, "SHARED_RECOVERY_MIN_DRAWDOWN"), cash * setting_float(settings, "SHARED_RECOVERY_MIN_DRAWDOWN_PCT")))
    campaign_target_profit = money(drawdown * setting_float(settings, "SHARED_RECOVERY_TARGET_PROFIT_MULTIPLIER"))
    open_expected_profit = money(recovery_coordination.get("open_expected_profit"))
    target_profit = money(max(0.0, campaign_target_profit - open_expected_profit))
    slots_remaining = max(0, int(recovery_coordination.get("max_open_slices", 3)) - int(recovery_coordination.get("open_slices", 0)))
    slice_target_profit = money(target_profit / slots_remaining) if slots_remaining else 0.0
    configured_max = effective_cap(cash, setting_float(settings, "SHARED_RECOVERY_MAX_STAKE_PCT"), setting_float(settings, "SHARED_RECOVERY_MAX_STAKE_CAP"))
    slice_pct_cap = money(cash * setting_float(settings, "SHARED_RECOVERY_MAX_SLICE_STAKE_PCT"))
    max_stake = money(min(value for value in (configured_max, slice_pct_cap) if value > 0)) if configured_max > 0 and slice_pct_cap > 0 else money(configured_max or slice_pct_cap)
    total_cap = money(cash * setting_float(settings, "SHARED_RECOVERY_MAX_TOTAL_STAKE_PCT"))
    if total_cap > 0:
        max_stake = money(min(max_stake, max(0.0, total_cap - money(recovery_coordination.get("open_stake")))))
    context.update({
        "realized_profit_basis": realized,
        "drawdown": drawdown,
        "effective_min_drawdown": min_drawdown,
        "campaign_target_profit": campaign_target_profit,
        "open_expected_profit": open_expected_profit,
        "target_profit": target_profit,
        "slice_target_profit": slice_target_profit,
        "max_recovery_stake": max_stake,
        "max_open_slices": recovery_coordination.get("max_open_slices", 3),
        "open_slices": recovery_coordination.get("open_slices", 0),
        "slots_remaining": slots_remaining,
        "campaign_open_stake": recovery_coordination.get("open_stake", 0),
        "reason": "drawdown_below_min",
    })
    if drawdown >= min_drawdown and slice_target_profit > 0 and max_stake > 0:
        context["active"] = True
        context["reason"] = "shared_system_drawdown_recovery"
    return context


def recovery_quality_allocation(strategy, candidate, context, settings=None):
    """Allocate 40/70/100% of remaining recovery profit from composite bet quality."""
    settings = settings or load_settings()
    candidate = candidate or {}
    context = context or {}

    def normalized(value, floor, elite):
        value = float(value or 0)
        floor = float(floor or 0)
        elite = max(floor + 0.01, float(elite or 0))
        return min(1.0, max(0.0, (value - floor) / (elite - floor)))

    strategy = str(strategy or "crypto").lower()
    edge = float(candidate.get("edge") or 0)
    confidence = float(candidate.get("confidence") or candidate.get("confidence_score") or 0)
    components = {}
    if strategy == "sports":
        pro_score = float((candidate.get("pro_review") or {}).get("score") or candidate.get("pro_score") or 0)
        final_score = float(candidate.get("final_bet_score") or (candidate.get("final_score_review") or {}).get("score") or 0)
        components = {
            "confidence": normalized(confidence, setting_float(settings, "SPORTS_RECOVERY_BASKET_MIN_CONFIDENCE") or 80, setting_float(settings, "SHARED_RECOVERY_SPORTS_ELITE_CONFIDENCE")),
            "edge": normalized(edge, setting_float(settings, "SPORTS_RECOVERY_BASKET_MIN_EDGE") or 4, setting_float(settings, "SHARED_RECOVERY_SPORTS_ELITE_EDGE")),
            "pro_score": normalized(pro_score, setting_float(settings, "SPORTS_RECOVERY_BASKET_MIN_PRO_SCORE") or 100, setting_float(settings, "SHARED_RECOVERY_SPORTS_ELITE_PRO_SCORE")),
            "final_score": normalized(final_score, setting_float(settings, "SPORTS_RECOVERY_BASKET_MIN_FINAL_SCORE") or 95, setting_float(settings, "SHARED_RECOVERY_SPORTS_ELITE_FINAL_SCORE")),
        }
        quality = 100.0 * (
            components["confidence"] * 0.30
            + components["edge"] * 0.25
            + components["pro_score"] * 0.20
            + components["final_score"] * 0.25
        )
    else:
        components = {
            "confidence": normalized(confidence, setting_float(settings, "CRYPTO_SHARED_RECOVERY_MIN_CONFIDENCE") or 80, setting_float(settings, "SHARED_RECOVERY_CRYPTO_ELITE_CONFIDENCE")),
            "edge": normalized(edge, setting_float(settings, "CRYPTO_SHARED_RECOVERY_MIN_EDGE") or 12, setting_float(settings, "SHARED_RECOVERY_CRYPTO_ELITE_EDGE")),
        }
        quality = 100.0 * (components["confidence"] * 0.55 + components["edge"] * 0.45)

    elite_score = setting_float(settings, "SHARED_RECOVERY_ELITE_QUALITY_SCORE") or 85
    strong_score = setting_float(settings, "SHARED_RECOVERY_STRONG_QUALITY_SCORE") or 60
    if quality >= elite_score:
        tier = "elite_full"
        fraction = setting_float(settings, "SHARED_RECOVERY_ELITE_ALLOCATION") or 1.0
    elif quality >= strong_score:
        tier = "strong_partial"
        fraction = setting_float(settings, "SHARED_RECOVERY_STRONG_ALLOCATION") or 0.70
    else:
        tier = "qualified_partial"
        fraction = setting_float(settings, "SHARED_RECOVERY_QUALIFIED_ALLOCATION") or 0.40
    fraction = min(1.0, max(0.0, fraction))
    remaining_target = money(context.get("target_profit"))
    return {
        "strategy": strategy,
        "tier": tier,
        "quality_score": round(quality, 1),
        "quality_components": {key: round(value * 100.0, 1) for key, value in components.items()},
        "allocation_fraction": round(fraction, 2),
        "remaining_target_profit": remaining_target,
        "target_profit": money(remaining_target * fraction),
    }


@serialized_shared_state
def reserve_live_order(strategy, stake, price_cents=None, metadata=None):
    settings = load_settings()
    metadata = metadata or {}
    parent_budget = strategy == "sports" and metadata.get("parent_capper_ticket_id")
    if parent_budget and Path(STATE_FILE).exists():
        parent_state = read_json(STATE_FILE, None)
        if not isinstance(parent_state, dict) or not isinstance(parent_state.get("capper_parent_commitments", {}), dict):
            return {"ok": False, "approved_stake": 0.0, "error": "capper_parent_reservation_state_unavailable"}
    review = live_order_review(strategy, stake, price_cents, settings, metadata=metadata)
    if not review.get("ok"):
        return review
    if not setting_bool(settings, "SHARED_BANKROLL_RESERVATIONS_ENABLED"):
        if parent_budget:
            return {"ok": False, "error": "capper_parent_reservations_required"}
        return {**review, "reservation_id": None}
    stored = read_json(STATE_FILE, {"reservations": [], "reservation_history": []})
    if parent_budget:
        parent_portfolio = read_json(SPORTS_PORTFOLIO_FILE, None)
        if not isinstance(parent_portfolio, dict) or not isinstance(parent_portfolio.get("bets"), list) or not isinstance(parent_portfolio.get("history"), list):
            return {"ok": False, "approved_stake": 0.0, "error": "capper_parent_portfolio_unavailable"}
        parent_review = capper_parent_exposure_review(parent_portfolio, stored.get("capper_parent_commitments") or {}, metadata, review["approved_stake"])
        if not parent_review["ok"]:
            return {**parent_review, "approved_stake": 0.0}
    active = prune_reservations(stored)
    reservation_id = str(uuid.uuid4())
    ttl = max(10.0, setting_float(settings, "SHARED_RESERVATION_TTL_SECONDS"))
    reservation = {
        "id": reservation_id,
        "strategy": strategy,
        "stake": review["approved_stake"],
        "requested_stake": review["requested_stake"],
        "price_cents": price_cents,
        "status": "active",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "expires_at": (datetime.now().astimezone() + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
        "metadata": metadata or {},
    }
    stored["reservations"] = active + [reservation]
    if parent_budget:
        stored.setdefault("capper_parent_commitments", {})[reservation_id] = {
            "parent_capper_ticket_id": metadata["parent_capper_ticket_id"],
            "unit_size": float(metadata["unit_size"]),
            "units": float(review["approved_stake"]) / float(metadata["unit_size"]),
            "status": "reserved",
        }
    write_json(STATE_FILE, stored)
    return {**review, "reservation_id": reservation_id, "reservation": reservation}


@serialized_shared_state
def finalize_reservation(reservation_id, status, actual_stake=0.0):
    if not reservation_id:
        return
    stored = read_json(STATE_FILE, {"reservations": [], "reservation_history": []})
    commitment = (stored.get("capper_parent_commitments") or {}).get(reservation_id)
    if commitment:
        # Ambiguous orders are retained until recovery produces a definitive
        # result. Shared cash TTL expiry never releases a parent unit budget.
        if status in {"filled", "filled_pending_persist"}:
            commitment.update(units=max(0.0, float(actual_stake)) / commitment["unit_size"], status=status)
        elif status in {"not_filled", "order_not_found", "rejected_no_order"} or str(status).startswith("released_"):
            stored["capper_parent_commitments"].pop(reservation_id, None)
    active = []
    history = stored.get("reservation_history", [])[-250:]
    for reservation in stored.get("reservations", []):
        if reservation.get("id") == reservation_id:
            reservation["status"] = "active" if status == "filled_pending_persist" else status
            reservation["phase"] = status
            reservation["actual_stake"] = money(actual_stake)
            reservation["finalized_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            if status == "filled_pending_persist":
                reservation["stake"] = money(actual_stake)
                active.append(reservation)
            else:
                history.append(reservation)
        else:
            active.append(reservation)
    stored["reservations"] = active
    stored["reservation_history"] = history[-250:]
    write_json(STATE_FILE, stored)
