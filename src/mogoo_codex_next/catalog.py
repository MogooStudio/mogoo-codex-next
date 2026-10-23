"""Read a selected catalog without saving settings or invoking a model."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .config import validate_config
from .credentials import CredentialError, protect, resolve_key, valid_key

SOURCE_FIELDS = {"provider", "base_url", "api_key_env", "api_key", "codex_bin"}
MAX_CATALOG_BYTES = 1024 * 1024
CATALOG_ERRORS = {
    "catalog_key_missing": "请先填写中转 API Key 并保存，或手动输入模型 ID。",
    "catalog_key_invalid": "API Key 无效或无法读取，请重新填写，或手动输入模型 ID。",
    "catalog_auth": "中转模型目录鉴权失败，请检查密钥与权限，或手动输入模型 ID。",
    "catalog_unsupported": "中转服务未提供模型目录，请手动输入模型 ID。",
    "catalog_redirect": "中转模型目录发生重定向，请核对 API 地址或手动输入模型 ID。",
    "catalog_http": "中转服务暂时无法提供模型目录，请稍后重试或手动输入模型 ID。",
    "catalog_network": "无法读取中转模型目录，请检查 API 地址和网络，或手动输入模型 ID。",
    "catalog_size": "中转模型目录过大，请手动输入模型 ID。",
    "catalog_format": "中转返回了无法识别的模型目录，请手动输入模型 ID。",
}


class CatalogError(ValueError):
    """Only fixed public messages cross the MCP validation-error boundary."""
    def __init__(self, code):
        self.code = code if code in CATALOG_ERRORS else "catalog_network"
        super().__init__(CATALOG_ERRORS[self.code])


def catalog_config(source: dict, saved: dict | None = None) -> dict:
    if not isinstance(source, dict) or "provider" not in source or set(source) - SOURCE_FIELDS:
        raise ValueError("模型目录来源无效，请检查服务来源与 API 设置。")
    fields = dict(source)
    key = fields.pop("api_key", "")
    valid_key(key)
    config = validate_config(fields)
    if config["provider"] == "relay":
        if key:
            config["api_key_protected"] = protect(key)
            config["api_key_env"] = ""
        elif saved and config["base_url"].rstrip("/") == saved["base_url"].rstrip("/"):
            config["api_key_protected"] = saved.get("api_key_protected", "")
            config["api_key_env"] = saved.get("api_key_env", "")
        if not config["base_url"] or not (config["api_key_protected"] or config["api_key_env"]):
            raise ValueError("请先填写中转 API 地址和 API Key，再读取模型目录。")
    return config


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _catalog_endpoint(base_url: str) -> str:
    parts = urlsplit(base_url)
    # These documented Codex gateways expose their catalog on the general API.
    # Preserve the selected origin and the generation configuration verbatim.
    if (parts.scheme == "https" and parts.hostname in
            {"api.aicodewith.ai", "api.aicodewith.com", "api.with7.cn"}
            and parts.path.rstrip("/") == "/chatgpt/v1"):
        return urlunsplit((parts.scheme, parts.netloc, "/v1/models", "", ""))
    return base_url.rstrip("/") + "/models"


def relay_models(config: dict) -> list:
    try:
        key = resolve_key(config)
    except CredentialError:
        raise CatalogError("catalog_key_invalid") from None
    if not key:
        raise CatalogError("catalog_key_missing")
    request = urllib.request.Request(_catalog_endpoint(config["base_url"]),
                                     headers={"Authorization": "Bearer " + key, "Accept": "application/json",
                                              "User-Agent": "Mogoo-Codex-Next/" + __version__},
                                     method="GET")
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=10) as response:
            raw = response.read(MAX_CATALOG_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status in (401, 403):
            raise CatalogError("catalog_auth") from None
        if status in (404, 405):
            raise CatalogError("catalog_unsupported") from None
        if 300 <= status < 400:
            raise CatalogError("catalog_redirect") from None
        raise CatalogError("catalog_http") from None
    except (OSError, ValueError):
        raise CatalogError("catalog_network") from None
    if len(raw) > MAX_CATALOG_BYTES:
        raise CatalogError("catalog_size")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CatalogError("catalog_format") from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise CatalogError("catalog_format")
    result, seen = [], set()
    for item in data["data"]:
        model = item.get("id") if isinstance(item, dict) else None
        if (isinstance(model, str) and 0 < len(model) <= 256 and model == model.strip()
                and not any(ord(char) < 32 for char in model) and model not in seen):
            result.append({"id": model})
            seen.add(model)
    if data["data"] and not result:
        raise CatalogError("catalog_format")
    return result
