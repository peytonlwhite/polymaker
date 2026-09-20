"""Run unittest discovery without external networking or writes to live paths.

Use from an isolated checkout with no credentials or copied runtime state:
    python -B tools/run_isolated_tests.py
"""
import ipaddress
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path.cwd().resolve()
TEMP = Path(tempfile.gettempdir()).resolve()
sys.path.insert(0, str(ROOT))


def check_write(path):
    if isinstance(path, int) or path is None:
        return
    resolved = Path(os.fsdecode(path)).resolve()
    if not (resolved.is_relative_to(ROOT) or resolved.is_relative_to(TEMP)):
        raise PermissionError("Test write outside isolated checkout/temp directory")


def guard(event, args):
    if event == "open":
        flags = args[2] if len(args) > 2 else 0
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            check_write(args[0])
    elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate"}:
        check_write(args[0])
    elif event in {"os.rename", "os.replace"}:
        check_write(args[0])
        check_write(args[1])
    elif event in {"socket.connect", "socket.sendto"}:
        address = args[-1]
        host = address[0] if isinstance(address, tuple) else ""
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = host == "localhost"
        if not allowed:
            raise PermissionError("External network disabled during isolated tests")
    elif event in {"os.system", "subprocess.Popen"}:
        raise PermissionError("Unmocked subprocess disabled during isolated tests")


sys.addaudithook(guard)
suite = (
    unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
    if len(sys.argv) > 1
    else unittest.defaultTestLoader.discover(str(ROOT), pattern="test_*.py")
)
result = unittest.TextTestRunner(verbosity=1).run(suite)
summary = {"tests_run": result.testsRun, "failures": [test.id() for test, _ in result.failures], "errors": [test.id() for test, _ in result.errors], "skipped": [(test.id(), reason) for test, reason in result.skipped], "successful": result.wasSuccessful(), "external_network": "blocked", "writes": "checkout_and_temp_only"}
(ROOT / "audit_test_results.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
raise SystemExit(0 if result.wasSuccessful() else 1)
