"""Read-only worker identity checks, independent of launcher status or shells."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import re

WORKERS = {"sports": (".sports_bot.pid", "sports bot"),
           "crypto": (".crypto_bot.pid", "crypto bot")}


def native_process(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"running": False, "reason": "invalid_pid"}
    if pid <= 0:
        return {"running": False, "reason": "invalid_pid"}
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return {"running": True, "image": str(Path(f"/proc/{pid}/exe").resolve()), "created_at": None}
        except ProcessLookupError:
            return {"running": False, "reason": "process_exited"}
        except OSError as exc:
            return {"running": None, "reason": type(exc).__name__}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        return {"running": False if code == 87 else None, "reason": f"process_query_error_{code}"}
    try:
        waited = kernel.WaitForSingleObject(handle, 0)
        if waited == 0:
            return {"running": False, "reason": "process_exited"}
        if waited != 258:
            return {"running": None, "reason": "process_wait_failed"}
        size = wintypes.DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) or not kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            return {"running": None, "reason": "process_identity_unavailable"}
        ticks = times[0].dwHighDateTime * 2**32 + times[0].dwLowDateTime
        return {"running": True, "image": image.value, "created_at": ticks / 10_000_000 - 11644473600}
    finally:
        kernel.CloseHandle(handle)


def worker_status(name, root=Path("."), probe=native_process):
    filename, label = WORKERS[name]
    path = Path(root) / filename
    if not path.exists():
        return {"running": False, "status": "offline", "reason": "worker_guard_missing", "pid": None}
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
        pid = int(record["pid"])
        started = datetime.fromisoformat(record["started_at"])
        if record.get("label") != label or not started.tzinfo:
            raise ValueError("invalid_worker_identity")
    except (OSError, ValueError, KeyError, TypeError):
        return {"running": None, "status": "unknown", "reason": "worker_guard_unreadable", "pid": None}
    result = probe(pid)
    if result["running"] is not True:
        return {**result, "pid": pid, "status": "unknown" if result["running"] is None else "offline"}
    executable = str(result.get("image") or "").replace("\\", "/").split("/")[-1]
    created = result.get("created_at")
    if not re.fullmatch(r"python(?:w|[0-9]+(?:\.[0-9]+)*)?(?:\.exe)?", executable, re.I):
        return {"running": None, "status": "unknown", "pid": pid, "reason": "worker_executable_mismatch"}
    if created is None or not -2 <= started.timestamp() - created <= 120:
        return {"running": None, "status": "unknown", "pid": pid, "reason": "worker_start_time_mismatch"}
    return {"running": True, "status": "running", "pid": pid, "started_at": started.isoformat(),
            "stopped_at": None, "identity_verified": True}
