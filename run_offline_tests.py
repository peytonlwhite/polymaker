"""Run unittest with a temporary cwd, no network/process launches or repo writes.

Usage: python -B run_offline_tests.py [test_module ...]
Without names this runs the repository's complete test_*.py suite.
"""
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
import shutil


def main():
    repository = Path(__file__).resolve().parent
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(repository))
    def protected(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        try:
            return Path(os.fsdecode(value)).resolve().is_relative_to(repository)
        except (ValueError, OSError):
            return False
    def guard(event, args):
        if event == "socket.connect":
            caller = sys._getframe(1)
            address = args[1] if len(args) > 1 else None
            # Windows asyncio builds an internal self-pipe using socketpair.
            # Permit only that stdlib call, never arbitrary loopback services.
            if (caller.f_code.co_name == "_fallback_socketpair"
                    and Path(caller.f_code.co_filename) == Path(__import__("socket").__file__)
                    and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}):
                return
        if event in {"socket.connect", "socket.connect_ex", "subprocess.Popen", "os.system", "os.kill"}:
            raise PermissionError("offline_validation_blocked:" + event)
        if event == "open":
            path, mode, flags = args
            write = any(c in (mode or "") for c in "wax+") or bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if write and protected(path):
                raise PermissionError("offline_validation_repo_write:" + str(path))
        if event in {"os.remove", "os.rmdir", "os.rename", "os.mkdir"} and any(protected(p) for p in args[:2]):
            raise PermissionError("offline_validation_repo_mutation:" + event)
    with tempfile.TemporaryDirectory(prefix="polymaker-offline-tests-") as work:
        for source in repository.glob("*.ps1"):
            shutil.copyfile(source, Path(work) / source.name)
        os.chdir(work)
        sys.addaudithook(guard)
        try:
            suite = (unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv) > 1
                     else unittest.defaultTestLoader.discover(str(repository), pattern="test_*.py"))
            result = unittest.TextTestRunner(verbosity=1).run(suite)
            print("OFFLINE_TEST_RESULT " + json.dumps({"run": result.testsRun, "failures": [str(t) for t, _ in result.failures], "errors": [str(t) for t, _ in result.errors]}))
            return 0 if result.wasSuccessful() else 1
        finally:
            os.chdir(repository)


if __name__ == "__main__":
    raise SystemExit(main())
