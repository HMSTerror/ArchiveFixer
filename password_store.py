"""Five password presets, encrypted for the current Windows user with DPAPI."""

from __future__ import annotations

import json
import os
from pathlib import Path
import base64
import ctypes
from ctypes import wintypes
import tempfile


def _crypt(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("密码加密保存需要 Windows DPAPI。")

    class DataBlob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DataBlob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel32.LocalFree(output.data)


def default_store_path() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    return root / "ArchiveFixer" / "password-presets.json"


def load_password_presets(path: Path, default_password: str) -> list[str]:
    presets = [default_password, "", "", "", ""]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return presets
    if isinstance(data, dict) and data.get("protection") == "windows-dpapi":
        try:
            encrypted = base64.b64decode(data["data"], validate=True)
            data = json.loads(_crypt(encrypted, decrypt=True).decode("utf-8"))
        except (KeyError, OSError, ValueError, TypeError):
            return presets
    # Read legacy plaintext files; an explicit Save converts them to DPAPI.
    values = data.get("passwords") if isinstance(data, dict) else None
    if isinstance(values, list) and len(values) == 5 and all(isinstance(value, str) for value in values):
        return values
    return presets


def save_password_presets(path: Path, passwords: list[str]) -> None:
    if len(passwords) != 5 or any(not isinstance(value, str) for value in passwords):
        raise ValueError("必须提供 5 个密码预设。")
    encrypted = _crypt(json.dumps({"passwords": passwords}, ensure_ascii=False).encode("utf-8"))
    document = {"version": 2, "protection": "windows-dpapi", "data": base64.b64encode(encrypted).decode("ascii")}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".password-presets-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
