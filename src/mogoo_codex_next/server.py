"""Dependency-free, newline-delimited MCP stdio transport and app resources."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

from . import __version__

RESOURCE_URI = "ui://mogoo-codex-next/panel.html"
MIME_TYPE = "text/html;profile=mcp-app"
# This server implements the established stdio tools/resources protocol. MCP
# 2026-07-28 discovery/result envelopes are intentionally not advertised.
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def tool(name: str, description: str, properties: dict | None = None,
         required: list[str] | None = None, *, panel: bool = False,
         readonly: bool = False, open_world: bool = False) -> dict:
    result: dict[str, Any] = {
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": properties or {},
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": readonly, "destructiveHint": False,
                        "openWorldHint": open_world},
        "_meta": {"ui": {"visibility": ["app"]}},
    }
    if required:
        result["inputSchema"]["required"] = required
    if panel:
        result.update(title="下一步建议")
        result["_meta"] = {
            "ui": {"resourceUri": RESOURCE_URI, "visibility": ["model", "app"]},
            "openai/ui": {"entrypoints": [{"type": "thread"}]},
        }
    return result


PANEL_PROPERTY = {"panel_id": {"type": "string", "minLength": 1, "maxLength": 128}}
TOOLS = [
    tool("next_panel", "打开当前聊天右侧的下一步建议面板。首次使用请在面板中选择独立模型；面板打开且开启自动生成时会产生普通模型用量。", panel=True),
    tool("next_poll", "为当前聊天面板续租并读取建议；符合条件时在后台生成本轮建议。", PANEL_PROPERTY, ["panel_id"]),
    tool("next_close", "关闭该面板的生成租约；不影响其他面板。", PANEL_PROPERTY, ["panel_id"]),
    tool("next_refresh", "用户明确点击重新生成时，为当前聊天请求一次新的建议，产生普通模型用量。", PANEL_PROPERTY, ["panel_id"]),
    tool("next_auto", "设置该面板的自动生成开关。", {**PANEL_PROPERTY, "enabled": {"type": "boolean"}}, ["panel_id", "enabled"]),
    tool("next_settings", "读取插件独立模型设置和密钥是否已配置，不返回 API Key。", readonly=True),
    tool("next_configure", "保存用户在面板中选择的独立模型设置，不修改 Codex 或其他插件的配置。", {"changes": {"type": "object"}}, ["changes"]),
    tool("next_models", "按当前选择的服务来源读取模型目录；中转使用表单 API 地址与填写或已保存的 API Key，不保存设置、不发起推理。", {
        "source": {"type": "object", "properties": {
            "provider": {"type": "string", "enum": ["official", "relay"]},
            "base_url": {"type": "string"}, "api_key": {"type": "string", "maxLength": 8192, "writeOnly": True},
            "codex_bin": {"type": ["string", "null"]}},
            "required": ["provider"], "additionalProperties": False}}, readonly=True, open_world=True),
    tool("next_appearance", "读取或保存本机全局皮肤，不改变模型、建议或生成次数。", {"skin": {"type": "string", "enum": ["graphite", "annotation", "focus"]}}),
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        super().__init__(message)


def resource_html(project_root: Path) -> str:
    ui = project_root / "ui"
    html = (ui / "panel.html").read_text(encoding="utf-8")
    css = (ui / "panel.css").read_text(encoding="utf-8")
    js = (ui / "panel.js").read_text(encoding="utf-8")
    html = re.sub(r'<link\b[^>]*href=[\"\'](?:\./)?panel\.css[\"\'][^>]*>',
                  lambda _: "<style>" + css + "</style>", html)
    html = re.sub(r'<script\b[^>]*src=[\"\'](?:\./)?panel\.js[\"\'][^>]*>\s*</script>',
                  lambda _: '<script type="module">' + js.replace("</script", "<\\/script") + '</script>', html)
    return html


def bound_thread(meta: Any) -> str | None:
    """Use only the owning thread injected by the host, never request arguments."""
    if not isinstance(meta, dict):
        return None
    value = meta.get("threadId")
    if isinstance(value, str) and ID_PATTERN.fullmatch(value):
        return value
    return None


class McpServer:
    def __init__(self, service: Any, project_root: Path):
        self.service = service
        self.project_root = project_root

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RpcError(-32602, "工具参数必须是对象")
        descriptor = next((item for item in TOOLS if item["name"] == name), None)
        if descriptor is None:
            raise RpcError(-32602, "未知工具")
        schema = descriptor["inputSchema"]
        if set(arguments) - set(schema["properties"]):
            raise RpcError(-32602, "存在不支持的工具参数")
        if any(key not in arguments for key in schema.get("required", [])):
            raise RpcError(-32602, "缺少工具参数")
        thread_id = bound_thread(params.get("_meta"))
        if name == "next_panel":
            if thread_id:
                data = self.service.open_panel(thread_id)
            else:
                data = {"panel_id": None, "thread_id": None, "status": "unbound",
                        "title": "", "auto_generate": False, "suggestions": [],
                        "usage": {}, "config": self.service.get_settings(),
                        "error": "宿主未提供聊天标识，请重新打开面板或更新 Codex。", "revision": 0}
        elif name in {"next_poll", "next_close", "next_refresh", "next_auto"}:
            if not thread_id:
                raise RpcError(-32602, "缺少宿主聊天标识，已停止生成")
            panel_id = arguments.get("panel_id")
            if not isinstance(panel_id, str) or not ID_PATTERN.fullmatch(panel_id):
                raise RpcError(-32602, "无效面板标识")
            if name == "next_auto":
                if type(arguments.get("enabled")) is not bool:
                    raise RpcError(-32602, "enabled 必须是布尔值")
                data = self.service.set_auto(panel_id, thread_id, arguments["enabled"])
            else:
                method = {"next_poll": "poll", "next_close": "close_panel", "next_refresh": "refresh"}[name]
                data = getattr(self.service, method)(panel_id, thread_id)
        elif name == "next_appearance":
            if "skin" in arguments:
                if arguments["skin"] not in ("graphite", "annotation", "focus"):
                    raise RpcError(-32602, "无效皮肤")
                data = {"appearance": self.service.set_appearance(arguments["skin"])}
            else:
                data = {"appearance": self.service.get_appearance()}
        elif name == "next_settings":
            data = {"config": self.service.get_settings()}
        elif name == "next_configure":
            if not isinstance(arguments.get("changes"), dict):
                raise RpcError(-32602, "changes 必须是对象")
            data = {"config": self.service.configure(arguments["changes"])}
        else:
            source = arguments.get("source")
            if "source" in arguments and not isinstance(source, dict):
                raise ValueError("模型目录来源必须是对象。")
            models = self.service.models(source)
            provider = source["provider"] if source is not None else self.service.get_settings()["provider"]
            data = {"models": models, "provider": provider}
        data = {**data, "plugin_version": __version__}
        result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False),
                               "annotations": {"audience": ["assistant"]}}], "structuredContent": data}
        if name == "next_panel":
            result["_meta"] = {"ui": {"resourceUri": RESOURCE_URI}}
        return result

    def dispatch(self, request: dict) -> dict | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            raise RpcError(-32600, "无效 JSON-RPC 请求")
        method = request.get("method")
        if "id" not in request:
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise RpcError(-32602, "params 必须是对象")
        if method == "initialize":
            requested = params.get("protocolVersion")
            return {"protocolVersion": requested if requested in PROTOCOLS else "2025-11-25",
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "mogoo-codex-next", "version": __version__},
                    "instructions": "建议面板通过当前聊天右侧入口打开。后台模型由插件独立配置；采纳只填入草稿，由用户发送。"}
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            try:
                return self._call_tool(params)
            except RpcError:
                raise
            except (ValueError, PermissionError) as exc:
                return {"isError": True, "content": [{"type": "text", "text": str(exc)}]}
            except Exception:
                # Never return subprocess stderr or environment values to the UI/model.
                return {"isError": True, "content": [{"type": "text", "text": "插件操作失败，请检查配置后重试。"}]}
        if method == "resources/list":
            return {"resources": [{"uri": RESOURCE_URI, "name": "next-steps-panel", "mimeType": MIME_TYPE}]}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "resources/read":
            if params.get("uri") != RESOURCE_URI:
                raise RpcError(-32602, "未知资源")
            return {"contents": [{"uri": RESOURCE_URI, "mimeType": MIME_TYPE,
                                  "text": resource_html(self.project_root),
                                  "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []},
                                                    "permissions": {"clipboardWrite": {}}}}}]}
        raise RpcError(-32601, "不支持的请求方法")

    def run(self, source: TextIO = sys.stdin, target: TextIO = sys.stdout) -> None:
        try:
            for line in source:
                if not line.strip():
                    continue
                request: Any = None
                try:
                    if len(line) > 2_000_000:
                        raise RpcError(-32600, "请求过大")
                    try:
                        request = json.loads(line)
                    except json.JSONDecodeError:
                        raise RpcError(-32700, "JSON 格式错误") from None
                    result = self.dispatch(request)
                    if isinstance(request, dict) and "id" not in request:
                        continue
                    response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                except RpcError as exc:
                    response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None,
                                "error": {"code": exc.code, "message": str(exc)}}
                target.write(json.dumps(response, ensure_ascii=False) + "\n")
                target.flush()
        finally:
            self.service.shutdown()
