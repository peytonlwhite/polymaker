"""Resume the existing Crypto shadow cohort with its exact retained source.

Research rules and state stay untouched. Source lives in an immutable archive;
data, existing locks and reports stay in the original workspace. No live API
adapter or trading entry point is available to this public-data collector.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
FILES = ("crypto_shadow_worker.py", "crypto_shadow_expansion.py", "crypto_shadow_challenger.py",
         "crypto_15m_learning.py", "crypto_scan_intelligence.py", "crypto_execution_safety.py",
         "crypto_pricing.py", "crypto_paper_bettor.py", "crypto_microstructure.py")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class SourceAndDataRoot:
    def __init__(self, workspace, source):
        self.workspace, self.source = Path(workspace), Path(source)

    def __fspath__(self):
        return str(self.workspace)

    def __truediv__(self, name):
        # The worker hashes the files it actually imports, not current live-bot
        # source. Every mutable path remains the existing cohort's own path.
        return (self.source if name in FILES else self.workspace) / name


def verified_source(root=ROOT):
    state = json.loads((root / "crypto_shadow_expansion.json").read_text(encoding="utf-8-sig"))
    expected = state.get("implementation_hash")
    if state.get("mode") != "shadow_only" or not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("Existing shadow cohort identity is invalid; preserve it for review.")
    source = root / "archives" / "frozen_crypto_shadow" / expected / "source"
    actual = digest({name: digest((source / name).read_text(encoding="utf-8")) for name in FILES})
    if actual != expected:
        raise ValueError("Retained source does not match the frozen research hash.")
    manifest = json.loads((source.parent / "manifest.json").read_text(encoding="utf-8"))
    for name, checksum in manifest["files"].items():
        if Path(name).name != name or not name.endswith(".py"):
            raise ValueError("Invalid retained source manifest path.")
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("Retained dependency changed: " + name)
    if not set(FILES).issubset(manifest["files"]):
        raise ValueError("Retained source manifest is incomplete.")
    return source, expected


def deny_live_access(*args, **kwargs):
    raise RuntimeError("Frozen shadow recovery cannot call a private account or live-order API")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        source, identity = verified_source()
    except (OSError, ValueError, KeyError) as exc:
        print("Frozen shadow source needs review: " + str(exc), file=sys.stderr, flush=True)
        return 42  # Permanent configuration failure; do not restart every 10s.
    if args.check:
        print(json.dumps({"ok": True, "implementation_hash": identity, "source": str(source)}))
        return 0
    sys.path.insert(0, str(source))
    import kalshi_common
    kalshi_common.kalshi_private_request = deny_live_access
    import crypto_paper_bettor as bot
    if Path(bot.__file__).resolve() != (source / "crypto_paper_bettor.py").resolve():
        raise RuntimeError("Unexpected shadow quote adapter source")
    bot.kalshi_private_request = deny_live_access
    bot.place_live_kalshi_order = deny_live_access
    spec = importlib.util.spec_from_file_location("frozen_crypto_shadow_worker", source / "crypto_shadow_worker.py")
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    worker.ROOT = SourceAndDataRoot(ROOT, source)
    return worker.run()


if __name__ == "__main__":
    raise SystemExit(main())
