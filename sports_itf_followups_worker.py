"""Observe the local ITF shadow ledger; no network or order interface."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from runtime_guard import acquire_single_instance, AlreadyRunningError
from sports_itf_shadow import Experiment, atomic_json, now_central
from sports_itf_followups import Followups, REPORT, STATE


def launch(root=None, *, once=False, register=False):
    root = Path(root or Path(__file__).resolve().parent)
    try:
        acquire_single_instance(root / ".sports_itf_followups_bot.pid", "ITF shadow follow-up tests")
    except AlreadyRunningError:
        return 0
    digest = hashlib.sha256(b"".join((root / name).read_bytes() for name in
                                   ("sports_itf_followups.py", "sports_itf_followups_worker.py"))).hexdigest()
    while True:
        started = time.monotonic()
        try:
            source = Experiment(root / "sports_itf_shadow_state.json").state
            engine = Followups(root, source, register=register)
            if engine.state.get("implementation_hash") not in {None, digest}:
                raise ValueError("Follow-up implementation changed; frozen cohort requires review")
            engine.state["implementation_hash"] = digest
            engine.sync(source)
            engine.persist()
            report = engine.summary()
            report["worker"] = {"pid": os.getpid(), "error": None, "local_read_only_source": True,
                                "poll_seconds": 60, "implementation_hash": digest}
            atomic_json(root / REPORT, report)
            register = False
            print(json.dumps({"at": report["generated_at"], "status": report["status"],
                              "entries": sum(r["entries"] for r in report["strategies"])}), flush=True)
            if once or report["status"] == "complete":
                return 0
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)[:240]
            print(json.dumps({"at": now_central().isoformat(), "error": error}), flush=True)
            # Preserve the ledger. Failed first registration must not create a report
            # that could be confused with a successfully registered experiment.
            if (root / STATE).exists():
                atomic_json(root / REPORT, {"mode": "shadow", "status": "error",
                            "generated_at": now_central().isoformat(), "strategies": [],
                            "worker": {"pid": os.getpid(), "error": error}})
            if once or register:
                return 1
        time.sleep(max(2, 60 - (time.monotonic() - started)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--register", action="store_true", help="Register new prospective cohorts once")
    args = parser.parse_args()
    raise SystemExit(launch(once=args.once, register=args.register))
