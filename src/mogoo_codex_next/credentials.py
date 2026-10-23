"""Write-only API keys, protected by the current Windows account."""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
import os


class CredentialError(ValueError):
    pass


def valid_key(value: str) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise CredentialError("API Key 格式无效，请检查后重新填写。")
    return value


def _crypt(data: bytes, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise CredentialError("当前系统暂不支持加密保存 API Key，请在 Windows 上使用此功能。")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise CredentialError("无法保存或读取 API Key，请在当前 Windows 账户下重新填写。")
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if target.data:
            ctypes.memset(target.data, 0, target.size)
            kernel32.LocalFree(target.data)


def protect(value: str) -> str:
    valid_key(value)
    if not value:
        return ""
    return "dpapi:" + base64.b64encode(_crypt(value.encode("ascii"), False)).decode("ascii")


def unprotect(value: str) -> str:
    try:
        if not isinstance(value, str) or not value.startswith("dpapi:"):
            raise ValueError()
        raw = base64.b64decode(value[6:], validate=True)
        if not raw or len(raw) > 20000:
            raise ValueError()
        result = valid_key(_crypt(raw, True).decode("ascii"))
        if not result:
            raise ValueError()
        return result
    except (ValueError, UnicodeError, OSError):
        raise CredentialError("已保存的 API Key 无法读取，请重新填写并保存。") from None


def resolve_key(config: dict) -> str:
    if config.get("api_key_protected"):
        return unprotect(config["api_key_protected"])
    # Compatibility for settings saved by releases before direct-key entry.
    return valid_key(os.environ.get(config.get("api_key_env", ""), "").strip())
