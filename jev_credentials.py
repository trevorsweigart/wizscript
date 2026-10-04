"""Keep the Jev API key in a Windows user-encrypted file, outside the bundle."""

import base64
import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path


def credentials_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WizScript" / "jev_credentials.json"


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Use TYPESAFE_API_KEY on systems without Windows DPAPI.")
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    function = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    function.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    function.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; the key is tied to this Windows user.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Windows could not encrypt/decrypt the Jev credential.")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def save_api_key(key: str):
    key = key.strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("Enter a valid Jev API key.")
    payload = {"dpapi": base64.b64encode(_crypt(key.encode(), decrypt=False)).decode("ascii")}
    target = credentials_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(target)


def load_api_key() -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if key:
        return key
    target = credentials_path()
    if not target.exists():
        return ""
    try:
        encrypted = base64.b64decode(json.loads(target.read_text(encoding="utf-8"))["dpapi"], validate=True)
        return _crypt(encrypted, decrypt=True).decode()
    except Exception:
        raise RuntimeError("Cannot read the saved Jev key. Save it again for this Windows user.") from None
