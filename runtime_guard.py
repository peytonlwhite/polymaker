import atexit
import json
import os
from datetime import datetime
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    pass


def _pid_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.windll.kernel32
            kernel32.SetLastError(0)
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if handle:
                exit_code = ctypes.c_ulong(0)
                queried = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                kernel32.CloseHandle(handle)
                # Windows can keep an exited process object queryable while a
                # parent still holds a handle. Only STILL_ACTIVE means the PID
                # guard belongs to a running worker.
                return not queried or exit_code.value == 259
            return kernel32.GetLastError() == 5
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_single_instance(path, label):
    """Acquire a crash-safe PID-file guard for one long-running bot worker."""
    path = Path(path)
    payload = {
        "pid": os.getpid(),
        "label": str(label),
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    for _attempt in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception:
                existing = {}
            existing_pid = existing.get("pid")
            if _pid_running(existing_pid):
                raise AlreadyRunningError(
                    f"{label} already running with pid={existing_pid}; refusing duplicate worker"
                )
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)

            def release():
                try:
                    current = json.loads(path.read_text(encoding="utf-8-sig"))
                    if int(current.get("pid") or 0) == os.getpid():
                        path.unlink(missing_ok=True)
                except Exception:
                    pass

            atexit.register(release)
            return payload
    raise AlreadyRunningError(f"could not acquire {label} worker guard at {path}")
