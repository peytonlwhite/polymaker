"""Current-user encrypted secret storage for the Windows bot runtime."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import uuid
from ctypes import wintypes
from pathlib import Path


CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data):
    raw = bytes(data)
    buffer = ctypes.create_string_buffer(raw)
    return DATA_BLOB(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt(data, *, protect):
    if os.name != "nt":
        raise RuntimeError("dpapi_secret_store_requires_windows")
    input_blob, input_buffer = _blob(data)
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        ok = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "polymaker crypto secrets",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _atomic_write(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def write_secure_settings(path, values):
    payload = json.dumps(
        {str(key): str(value) for key, value in (values or {}).items() if str(value or "").strip()},
        sort_keys=True,
    ).encode("utf-8")
    protected = _crypt(payload, protect=True)
    wrapper = {
        "version": 1,
        "protection": "windows_dpapi_current_user",
        "ciphertext": base64.b64encode(protected).decode("ascii"),
    }
    _atomic_write(path, json.dumps(wrapper, indent=2, sort_keys=True).encode("utf-8"))


def read_secure_settings(path, default=None):
    path = Path(path)
    if not path.exists():
        return dict(default or {})
    wrapper = json.loads(path.read_text(encoding="utf-8-sig"))
    ciphertext = base64.b64decode(str(wrapper.get("ciphertext") or ""), validate=True)
    payload = json.loads(_crypt(ciphertext, protect=False).decode("utf-8"))
    return {str(key): str(value) for key, value in payload.items()}


def migrate_json_secrets(settings_path, secure_path, secret_names):
    """Move named non-empty values into DPAPI storage without plaintext backup."""
    settings_path = Path(settings_path)
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    existing = read_secure_settings(secure_path, {}) if Path(secure_path).exists() else {}
    moved = {}
    for name in secret_names:
        value = settings.get(name)
        if str(value or "").strip():
            moved[str(name)] = str(value)
            settings[name] = ""
    if not moved:
        return {"migrated": 0, "secure_path": str(secure_path)}
    write_secure_settings(secure_path, {**existing, **moved})
    _atomic_write(
        settings_path,
        json.dumps(settings, indent=2, sort_keys=True).encode("utf-8"),
    )
    return {"migrated": len(moved), "secure_path": str(secure_path)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("settings_path")
    parser.add_argument("secure_path")
    parser.add_argument("secret_names", nargs="+")
    args = parser.parse_args(argv)
    result = migrate_json_secrets(args.settings_path, args.secure_path, args.secret_names)
    print(f"Migrated {result['migrated']} secret fields to DPAPI storage.")


if __name__ == "__main__":
    main()
