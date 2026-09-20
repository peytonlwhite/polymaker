"""Content identity of the sports code loaded by this process; no git or secrets."""

import hashlib
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo


CORE_FILES = (
    "kalshi_common.py", "shared_bankroll.py", "live_core.py", "shadow_pricing.py",
    "ai_voting.py", "bot_pick_bridge.py", "runtime_guard.py", "secure_settings.py",
    "aibetpicks_accounting.py", "shared_cash_pool.py",
)


def source_identity(root):
    root = Path(root)
    names = sorted({path.name for path in root.glob("sports_*.py")} | set(CORE_FILES))
    manifest = {}
    for name in names:
        path = root / name
        if path.is_file():
            # A checkout's newline conversion does not change its behavior.
            content = path.read_bytes().replace(b"\r\n", b"\n")
            manifest[name] = hashlib.sha256(content).hexdigest()
    digest = hashlib.sha256()
    for name, value in manifest.items():
        digest.update(f"{name}\0{value}\n".encode())
    return {"build_id": digest.hexdigest()[:20], "build_basis": "sports_source_sha256",
            "source_file_count": len(manifest)}


# Capture once, before the worker begins scanning. Editing files on disk must
# not relabel orders placed by an already-running, older process.
PROCESS_IDENTITY = {
    **source_identity(Path(__file__).resolve().parent),
    "process_started_at": datetime.now(ZoneInfo("America/Chicago")).isoformat(timespec="seconds"),
}


def strategy_fields(identity=None):
    identity = identity or PROCESS_IDENTITY
    return {"strategy_build_id": identity["build_id"],
            "strategy_process_started_at": identity["process_started_at"]}
