"""Bounded storage retention for Polymaker runtime data.

Operational logs are short-lived.  Durable event ledgers and analytics are
rotated and compressed but never expired by this module.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT = Path(__file__).resolve().parent
ARCHIVE_ROOT = ROOT / "archives" / "storage_retention"
STATE_FILE = ROOT / "storage_maintenance_state.json"
HEALTH_FILE = ROOT / "storage_health.json"
LOCK_FILE = ROOT / ".storage_maintenance.lock"
try:
    CENTRAL = ZoneInfo("America/Chicago")
except ZoneInfoNotFoundError:
    # Windows Store Python may not ship IANA tzdata. The host is configured for
    # Central time, so its local timezone remains the correct scheduling source.
    CENTRAL = datetime.now().astimezone().tzinfo

LOG_RETENTION_DAYS = 7
ERROR_RETENTION_DAYS = 14
TEMP_RETENTION_HOURS = 24
WARN_BYTES = 2 * 1024**3
CRITICAL_BYTES = 3 * 1024**3

ROTATION_LIMITS = {
    "sports_paper_log.txt": 50 * 1024**2,
    "sports_control.log": 50 * 1024**2,
    "sports_paper_events.jsonl": 100 * 1024**2,
    "sports_decision_snapshots.jsonl": 100 * 1024**2,
    "crypto_perps_shadow_events.jsonl": 100 * 1024**2,
    "crypto_15m_training_dataset.jsonl": 250 * 1024**2,
    "crypto_scan_intelligence.jsonl": 250 * 1024**2,
}


def _now():
    return datetime.now(CENTRAL)


def _category(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".jsonl") or ".jsonl." in name or "training_dataset" in name:
        return "analytics"
    if any(token in name for token in ("stderr", ".err", "error", "audit")):
        return "errors"
    return "logs"


def _archive_dir(path: Path) -> Path:
    try:
        path.resolve().relative_to(ROOT.resolve())
        base = ARCHIVE_ROOT
    except ValueError:
        # Tests, tools, and alternate worktrees may live on another Windows
        # volume. Keep their atomic rotation on the source volume.
        base = path.parent / "archives" / "storage_retention"
    return base / _category(path)


def _timestamp():
    return _now().strftime("%Y%m%dT%H%M%S_%f%z")


def _json_write(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _acquire_lock(max_age_seconds=1800):
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {_now().isoformat()}".encode("ascii", "replace"))
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - LOCK_FILE.stat().st_mtime > max_age_seconds:
                LOCK_FILE.unlink()
                return _acquire_lock(max_age_seconds)
        except OSError:
            pass
        return False


def _release_lock():
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def rotate_file(path, incoming_bytes=0, max_bytes=None):
    """Atomically roll an append-only file into a managed archive directory."""
    source = Path(path)
    limit = int(max_bytes or ROTATION_LIMITS.get(source.name, 0))
    if limit <= 0 or not source.exists():
        return None
    try:
        if source.stat().st_size + max(0, int(incoming_bytes or 0)) <= limit:
            return None
    except OSError:
        return None
    archive_dir = _archive_dir(source)
    archive_dir.mkdir(parents=True, exist_ok=True)
    rotated = archive_dir / f"{source.name}.{_timestamp()}"
    try:
        os.replace(source, rotated)
        return rotated
    except OSError:
        return None


def maybe_rotate_for_append(path, incoming_bytes=0):
    return rotate_file(path, incoming_bytes=incoming_bytes)


def _gzip_file(path: Path):
    if path.suffix == ".gz" or not path.is_file():
        return None
    target = path.with_name(path.name + ".gz")
    temp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        source_stat = path.stat()
        with path.open("rb") as source, gzip.open(temp, "wb", compresslevel=6) as dest:
            shutil.copyfileobj(source, dest, length=1024 * 1024)
        os.replace(temp, target)
        os.utime(target, (source_stat.st_atime, source_stat.st_mtime))
        path.unlink()
        return target
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        return None


def _managed_archives():
    if ARCHIVE_ROOT.exists():
        yield from (item for item in ARCHIVE_ROOT.rglob("*") if item.is_file())
    legacy = ROOT / "archives" / "crypto_log_rotation"
    if legacy.exists():
        yield from (item for item in legacy.iterdir() if item.is_file())


def _is_stale_temp(path: Path, cutoff: float):
    if not path.is_file() or path.stat().st_mtime >= cutoff:
        return False
    name = path.name.lower()
    return name.endswith(".tmp") and (name.startswith(".") or ".json." in name)


def _archive_timestamp(path: Path):
    match = re.search(r"\.(\d{8}T\d{6})(?:_\d+)?(Z|[+-]\d{4})(?:\.gz)?$", path.name)
    if not match:
        return path.stat().st_mtime
    stamp, zone = match.groups()
    try:
        if zone == "Z":
            parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.strptime(stamp + zone, "%Y%m%dT%H%M%S%z")
        return parsed.timestamp()
    except (ValueError, ZoneInfoNotFoundError):
        return path.stat().st_mtime


def _tree_size(path: Path):
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def storage_health(last_result=None):
    active = 0
    analytics = 0
    temp = 0
    for item in ROOT.iterdir():
        try:
            if not item.is_file():
                continue
            size = item.stat().st_size
            active += size
            if _category(item) == "analytics":
                analytics += size
            if item.name.lower().endswith(".tmp"):
                temp += size
        except OSError:
            pass
    archives = _tree_size(ROOT / "archives")
    total = active + archives
    status = "critical" if total >= CRITICAL_BYTES else "warning" if total >= WARN_BYTES else "healthy"
    previous = {}
    try:
        previous = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    report = {
        "generated_at": _now().isoformat(timespec="seconds"),
        "status": status,
        "total_bytes": total,
        "active_bytes": active,
        "analytics_bytes": analytics,
        "archive_bytes": archives,
        "temp_bytes": temp,
        "warning_bytes": WARN_BYTES,
        "critical_bytes": CRITICAL_BYTES,
        "policy": {
            "routine_logs_days": LOG_RETENTION_DAYS,
            "errors_audits_days": ERROR_RETENTION_DAYS,
            "stale_temp_hours": TEMP_RETENTION_HOURS,
            "analytics": "compressed and retained indefinitely",
            "critical_state": "validated .lastgood copy; never automatically deleted",
        },
        "last_cleanup": (last_result or {}).get("completed_at") or previous.get("last_cleanup"),
        "last_cleanup_result": last_result or previous.get("last_cleanup_result") or {},
    }
    _json_write(HEALTH_FILE, report)
    return report


def read_storage_health():
    """Return the cached report cheaply for the five-second dashboard refresh."""
    try:
        report = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        if isinstance(report, dict):
            return report
    except (OSError, json.JSONDecodeError):
        pass
    return storage_health()


def run_maintenance(force=True):
    if not _acquire_lock():
        return {"ok": False, "skipped": "maintenance_already_running"}
    started = _now()
    result = {
        "ok": True,
        "started_at": started.isoformat(timespec="seconds"),
        "rotated": [],
        "compressed": [],
        "deleted": [],
        "bytes_reclaimed": 0,
    }
    try:
        rotation_limits = dict(ROTATION_LIMITS)
        for item in ROOT.iterdir():
            if item.is_file() and (item.suffix.lower() == ".log" or item.name.lower().endswith("_log.txt")):
                rotation_limits.setdefault(item.name, 50 * 1024**2)
        for name, limit in rotation_limits.items():
            rotated = rotate_file(ROOT / name, max_bytes=limit)
            if rotated:
                result["rotated"].append(str(rotated.relative_to(ROOT)))

        # Compress both new managed archives and the Crypto bot's legacy rotations.
        for item in list(_managed_archives()):
            if item.suffix != ".gz":
                compressed = _gzip_file(item)
                if compressed:
                    result["compressed"].append(str(compressed.relative_to(ROOT)))

        now_ts = time.time()
        log_cutoff = now_ts - LOG_RETENTION_DAYS * 86400
        error_cutoff = now_ts - ERROR_RETENTION_DAYS * 86400
        for item in list(_managed_archives()):
            category = _category(item)
            cutoff = error_cutoff if category == "errors" else log_cutoff
            if category == "analytics":
                continue
            try:
                if _archive_timestamp(item) < cutoff:
                    size = item.stat().st_size
                    item.unlink()
                    result["bytes_reclaimed"] += size
                    result["deleted"].append(str(item.relative_to(ROOT)))
            except OSError:
                pass

        temp_cutoff = now_ts - TEMP_RETENTION_HOURS * 3600
        for item in ROOT.iterdir():
            try:
                if _is_stale_temp(item, temp_cutoff):
                    size = item.stat().st_size
                    item.unlink()
                    result["bytes_reclaimed"] += size
                    result["deleted"].append(item.name)
            except OSError:
                pass

        result["completed_at"] = _now().isoformat(timespec="seconds")
        result["rotated_count"] = len(result["rotated"])
        result["compressed_count"] = len(result["compressed"])
        result["deleted_count"] = len(result["deleted"])
        state = {"last_success": result["completed_at"], "last_result": result}
        _json_write(STATE_FILE, state)
        result["health"] = storage_health(dict(result))
        return result
    finally:
        _release_lock()


def scheduled_due(now=None):
    now = now or _now()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(state.get("last_success", ""))
        if last.tzinfo is None:
            last = last.replace(tzinfo=CENTRAL)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return True
    scheduled_time = now.replace(hour=3, minute=15, second=0, microsecond=0)
    missed_today = now >= scheduled_time and last.date() < now.date()
    startup_catchup = now - last > timedelta(hours=24)
    return missed_today or startup_catchup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheduled", action="store_true", help="run only when the daily job is due")
    parser.add_argument("--health", action="store_true", help="refresh and print storage health only")
    args = parser.parse_args()
    if args.health:
        payload = storage_health()
    elif args.scheduled and not scheduled_due():
        payload = {"ok": True, "skipped": "not_due"}
    else:
        payload = run_maintenance(force=True)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
