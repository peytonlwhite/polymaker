"""Hourly, deterministic health evaluation for the live sports process."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sports_adaptive_strategy import atomic_write_json
from sports_audit_support import CHICAGO, STRATEGY_VERSION, parse_timestamp as parse_time


VERSION = "sports-operations-guardian-v1"
EXPECTED_STRATEGY_VERSION = STRATEGY_VERSION
PID_FILE = Path(".sports_bot.pid")
REPORT_FILE = Path("sports_paper_report.json")
INTENTS_FILE = Path("sports_live_order_intents.json")
STATE_FILE = Path("sports_operations_guardian_state.json")
HEALTH_FILE = Path("sports_operations_health.json")
SETTINGS_FILE = Path("bot_settings.json")

ACTIVE_INTENT_STATUSES = {
    "prepared",
    "ambiguous",
    "response_received",
    "response_recovered",
    "filled_pending_portfolio",
}


def iso_now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(CHICAGO).isoformat(timespec="seconds")


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def process_exists(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _age_minutes(timestamp, now):
    parsed = parse_time(timestamp)
    return (now - parsed).total_seconds() / 60.0 if parsed else None


def _quiet_hours(settings, now):
    enabled = str(settings.get("SPORTS_QUIET_HOURS_ENABLED", "false")).lower() == "true"
    if not enabled:
        return False
    start = int(float(settings.get("SPORTS_QUIET_START_HOUR", 21)))
    end = int(float(settings.get("SPORTS_QUIET_END_HOUR", 9)))
    local_hour = now.astimezone(CHICAGO).hour
    return start <= local_hour or local_hour < end if start > end else start <= local_hour < end


def evaluate_health(*, pid_payload, report, intents, settings, state, now=None):
    now = now or datetime.now(timezone.utc)
    pid = pid_payload.get("pid") if isinstance(pid_payload, dict) else None
    running = process_exists(pid)
    report_at = report.get("generated_at") if isinstance(report, dict) else None
    report_age = _age_minutes(report_at, now)
    quiet = _quiet_hours(settings or {}, now)
    stale = bool(running and not quiet and (report_age is None or report_age > 45.0))
    stale_failures = int((state or {}).get("consecutive_stale_reports") or 0)
    stale_failures = stale_failures + 1 if stale else 0

    active_intents = []
    for row in (intents or {}).get("intents") or []:
        if str(row.get("status") or "") not in ACTIVE_INTENT_STATUSES:
            continue
        updated = row.get("updated_at") or row.get("created_at")
        age = _age_minutes(updated, now)
        # Age is diagnostic, never evidence that an ambiguous order resolved.
        # Keep the restart interlock until reconciliation changes its status.
        active_intents.append({
            "client_order_id": row.get("client_order_id"),
            "ticker": row.get("ticker"),
            "status": row.get("status"),
            "age_minutes": round(age, 2) if age is not None else None,
        })

    recent_restarts = []
    for value in (state or {}).get("restart_history") or []:
        parsed = parse_time(value)
        if parsed and now - parsed <= timedelta(hours=6):
            recent_restarts.append(value)
    restart_rate_limited = len(recent_restarts) >= 2
    restart_reason = ""
    if not running:
        restart_reason = "sports_process_missing"
    elif stale_failures >= 2:
        restart_reason = "sports_report_stale_twice"
    safe_to_restart = not active_intents and not restart_rate_limited
    if restart_reason and safe_to_restart:
        action = "restart"
    elif restart_reason:
        action = "alert"
    else:
        action = "none"

    observed_strategy = ((report.get("strategy_identity") or {}).get("version") if isinstance(report, dict) else None)
    warnings = []
    if running and observed_strategy and observed_strategy != EXPECTED_STRATEGY_VERSION:
        warnings.append("strategy_version_mismatch")
    if report_age is not None and report_age > 20 and not quiet:
        warnings.append("report_delayed")
    reconciliation = report.get("live_reconciliation") or {} if isinstance(report, dict) else {}
    execution_mode = str(report.get("execution_mode") or (settings or {}).get("SPORTS_EXECUTION_MODE") or "").lower()
    live_report = execution_mode == "live" or (not execution_mode and bool(reconciliation))
    live_warnings = []
    if live_report:
        if not (reconciliation.get("account") or {}).get("ok"):
            live_warnings.append("live_reconciliation_not_ok")
        if any(reconciliation.get(field) for field in (
            "position_mismatches", "unmatched_local_tickers", "unmatched_remote_tickers",
        )):
            live_warnings.append("live_reconciliation_mismatch")
        audit = report.get("live_audit") or {}
        if audit.get("ok") is False or audit.get("warnings"):
            live_warnings.append("live_audit_not_ok")
    warnings.extend(live_warnings)
    source = report.get("aibetpicks") or {} if isinstance(report, dict) else {}
    source_warning = None
    if source.get("enabled") and source.get("health") in {"error", "stale"}:
        source_warning = "aibetpicks_feed_" + source["health"]
        warnings.append(source_warning)
    if active_intents:
        warnings.append("unresolved_order_intents")
        if any(row["age_minutes"] is None or row["age_minutes"] > 10.0 for row in active_intents):
            warnings.append("stale_order_intents")
    if not restart_reason and (live_warnings or active_intents or source_warning):
        # Account/order and source-feed problems need review, not a blind restart.
        action = "alert"
    qualification_v2 = report.get("qualification_v2") or {} if isinstance(report, dict) else {}
    qualification_enabled = str((settings or {}).get("SPORTS_QUALIFICATION_V2_ENABLED", "false")).lower() == "true"
    if qualification_enabled and report and qualification_v2.get("version") != "sports-qualification-v2":
        warnings.append("qualification_v2_report_missing")
    if (
        int(qualification_v2.get("execution_active_count") or 0) > 0
        and not (qualification_v2.get("promoted_segments") or {})
    ):
        warnings.append("qualification_v2_execution_without_promoted_segment")

    health = {
        "version": VERSION,
        "generated_at": iso_now(now),
        "healthy": bool(running and not stale and not active_intents and not warnings),
        "action": action,
        "restart_reason": restart_reason,
        "safe_to_restart": safe_to_restart,
        "restart_rate_limited": restart_rate_limited,
        "process": {"pid": pid, "running": running},
        "report": {
            "generated_at": report_at,
            "age_minutes": round(report_age, 2) if report_age is not None else None,
            "stale": stale,
            "quiet_hours": quiet,
            "strategy_version": observed_strategy,
            "execution_mode": report.get("execution_mode") if isinstance(report, dict) else None,
        },
        "active_order_intents": active_intents,
        "warnings": warnings,
        "qualification_v2": {
            "version": qualification_v2.get("version"),
            "mode": qualification_v2.get("mode"),
            "execution_active_count": int(qualification_v2.get("execution_active_count") or 0),
            "promoted_segments": qualification_v2.get("promoted_segments") or {},
        },
        "consecutive_stale_reports": stale_failures,
        "restart_count_6h": len(recent_restarts),
    }
    next_state = {
        **(state or {}),
        "version": VERSION,
        "generated_at": iso_now(now),
        "consecutive_stale_reports": stale_failures,
        "restart_history": recent_restarts,
        "last_health": health,
    }
    return health, next_state


def check(now=None):
    health, state = evaluate_health(
        pid_payload=read_json(PID_FILE, {}),
        report=read_json(REPORT_FILE, {}),
        intents=read_json(INTENTS_FILE, {}),
        settings=read_json(SETTINGS_FILE, {}),
        state=read_json(STATE_FILE, {}),
        now=now,
    )
    atomic_write_json(STATE_FILE, state)
    atomic_write_json(HEALTH_FILE, health)
    return health


def record_restart(now=None):
    now = now or datetime.now(timezone.utc)
    state = read_json(STATE_FILE, {})
    history = [
        value
        for value in state.get("restart_history") or []
        if (parsed := parse_time(value)) and now - parsed <= timedelta(hours=6)
    ]
    history.append(iso_now(now))
    state.update({
        "generated_at": iso_now(now),
        "restart_history": history[-20:],
        "consecutive_stale_reports": 0,
        "last_restart_at": iso_now(now),
    })
    atomic_write_json(STATE_FILE, state)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--record-restart", action="store_true")
    args = parser.parse_args()
    payload = record_restart() if args.record_restart else check()
    print(json.dumps(payload, sort_keys=True))


if __name__ == "__main__":
    main()
