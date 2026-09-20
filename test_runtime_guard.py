import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import runtime_guard


class FakeKernel32:
    def __init__(self, error, handle=0, exit_code=259):
        self.error = error
        self.handle = handle
        self.exit_code = exit_code
        self.closed = []

    def SetLastError(self, _value):
        pass

    def OpenProcess(self, _access, _inherit, _pid):
        return self.handle

    def CloseHandle(self, handle):
        self.closed.append(handle)

    def GetExitCodeProcess(self, _handle, output):
        output.value = self.exit_code
        return 1

    def GetLastError(self):
        return self.error


class RuntimeGuardTests(unittest.TestCase):
    def test_windows_dead_pid_uses_actual_kernel_error(self):
        kernel32 = FakeKernel32(error=87)
        fake_ctypes = SimpleNamespace(
            windll=SimpleNamespace(kernel32=kernel32),
            get_last_error=lambda: 5,
        )
        with patch.object(runtime_guard.os, "name", "nt"), patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertFalse(runtime_guard._pid_running(987654))

    def test_windows_access_denied_pid_is_treated_as_running(self):
        kernel32 = FakeKernel32(error=5)
        fake_ctypes = SimpleNamespace(windll=SimpleNamespace(kernel32=kernel32))
        with patch.object(runtime_guard.os, "name", "nt"), patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertTrue(runtime_guard._pid_running(987654))

    def test_windows_queryable_exited_process_is_not_running(self):
        kernel32 = FakeKernel32(error=0, handle=123, exit_code=0)
        fake_ctypes = SimpleNamespace(
            windll=SimpleNamespace(kernel32=kernel32),
            c_ulong=lambda value=0: SimpleNamespace(value=value),
            byref=lambda value: value,
        )
        with patch.object(runtime_guard.os, "name", "nt"), patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            self.assertFalse(runtime_guard._pid_running(987654))
        self.assertEqual(kernel32.closed, [123])


if __name__ == "__main__":
    unittest.main()
