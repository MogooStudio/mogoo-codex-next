"""Independent settings with write-only, account-protected API credentials."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from urllib.parse import urlsplit

from .credentials import CredentialError, protect, resolve_key, valid_key

DEFAULTS = {
    "provider": "official", "model": "", "reasoning_effort": "low",
    "service_tier": None, "base_url": "", "api_key_env": "",
    "codex_bin": None, "auto_generate": True, "api_key_protected": "",
}
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
_LOCK = threading.RLock()


def data_dir() -> Path:
    value = os.environ.get("MOGOO_CODEX_NEXT_HOME")
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "mogoo-codex-next"


def validate_config(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("配置字段无效，请通过建议设置填写 API Key。")
    config = DEFAULTS | value
    protected = config["api_key_protected"]
    if not isinstance(protected, str) or len(protected) > 30000 or (protected and not protected.startswith("dpapi:")):
        raise ValueError("已保存的 API Key 格式无效，请重新填写。")
    if config["provider"] not in ("official", "relay"):
        raise ValueError("请选择官方或中转站")
    for key in ("model", "base_url", "api_key_env", "reasoning_effort"):
        text = config[key]
        if not isinstance(text, str) or text != text.strip() or any(ord(c) < 32 for c in text):
            raise ValueError("模型设置包含无效文本")
    if len(config["model"]) > 256:
        raise ValueError("模型名称过长")
    if config["reasoning_effort"] not in EFFORTS:
        raise ValueError("推理强度无效")
    tier = config["service_tier"]
    if tier in ("standard", ""):
        config["service_tier"] = None
    elif tier == "fast":
        config["service_tier"] = "priority"
    elif tier not in (None, "priority"):
        raise ValueError("服务档位只支持 standard 或 fast")
    if type(config["auto_generate"]) is not bool:
        raise ValueError("自动建议开关必须是布尔值")
    binary = config["codex_bin"]
    if binary is not None and (not isinstance(binary, str) or not binary.strip()
            or any(ord(c) < 32 for c in binary)):
        raise ValueError("Codex 程序路径无效")
    if config["api_key_env"] and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config["api_key_env"]):
        raise ValueError("密钥配置必须是环境变量名")
    if config["base_url"]:
        try:
            url = urlsplit(config["base_url"])
            valid = (url.scheme in ("https", "http") and url.hostname and url.port != 0
                     and not url.username and not url.password and not url.query and not url.fragment
                     and not any(c.isspace() for c in config["base_url"]))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("API 地址必须是 HTTP(S) 基础地址，不能含账号、密钥、查询或片段")
    return config


def load_config(root: Path) -> dict:
    path = Path(root) / "config.json"
    with _LOCK:
        try:
            if not path.exists():
                return dict(DEFAULTS)
            if path.stat().st_size > 64 * 1024:
                raise ValueError("配置文件过大")
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.pop("relay_source", None)  # 0.1.8 local-source option was withdrawn.
            return validate_config(value)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法读取插件配置") from None


def save_config(root: Path, changes: dict) -> dict:
    if not isinstance(changes, dict) or set(changes) - (set(DEFAULTS) - {"api_key_protected"} | {"api_key", "clear_api_key"}):
        raise ValueError("配置字段无效，请通过建议设置填写 API Key。")
    with _LOCK:
        changes = dict(changes)
        key = changes.pop("api_key", "")
        clear = changes.pop("clear_api_key", False)
        valid_key(key)
        if type(clear) is not bool or (clear and key):
            raise ValueError("请填写新 API Key 或清除旧 Key。")
        old = load_config(root)
        config = validate_config(old | changes)
        if key:
            config["api_key_protected"] = protect(key)
            config["api_key_env"] = ""
        elif clear:
            config["api_key_protected"] = ""
            config["api_key_env"] = ""
        elif (config["base_url"].rstrip("/") != old["base_url"].rstrip("/")
              and (old["api_key_protected"] or old["api_key_env"])):
            raise ValueError("API 地址已变更，请重新填写 API Key。")
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        fd, filename = tempfile.mkstemp(prefix=".config-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(config, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, root / "config.json")
        finally:
            if os.path.exists(filename):
                os.unlink(filename)
        return config


def public_config(config: dict) -> dict:
    config = validate_config(config)
    result = {key: value for key, value in config.items() if key not in {"api_key_protected", "api_key_env"}}
    try:
        result["api_key_present"] = bool(resolve_key(config))
        result["api_key_error"] = ""
    except CredentialError:
        result["api_key_present"] = False
        result["api_key_error"] = "已保存的 API Key 无法读取，请重新填写并保存。"
    return result


def config_fingerprint(config: dict) -> str:
    return hashlib.sha256(json.dumps(validate_config(config), sort_keys=True,
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def is_configured(config: dict) -> bool:
    config = validate_config(config)
    return bool(config["model"] and (config["provider"] == "official" or
                (config["base_url"] and (config["api_key_protected"] or config["api_key_env"]))))
