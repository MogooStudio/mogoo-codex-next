#!/usr/bin/env python3
"""Plugin entrypoint. Runs without installation or third-party dependencies."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))


def data_root() -> Path:
    override = os.environ.get("MOGOO_CODEX_NEXT_HOME")
    if override:
        return Path(override).expanduser().resolve()
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return codex_home / "mogoo-codex-next"


def main() -> int:
    parser = argparse.ArgumentParser(description="Mogoo Codex Next 插件")
    parser.add_argument("--data-dir", type=Path, help="覆盖插件数据目录（测试/便携使用）")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("serve", help="启动 MCP stdio 服务")
    subs.add_parser("hook", help="记录 Stop 事件，不发起推理")
    subs.add_parser("status", help="查看公开配置")
    doctor = subs.add_parser("doctor", help="检查入口与资源；可选探测 CLI")
    doctor.add_argument("--probe-cli", action="store_true", help="启动 CLI 读取模型目录，不调用模型")
    config = subs.add_parser("configure", help="设置插件独立模型")
    config.add_argument("--provider", choices=("official", "relay"))
    config.add_argument("--model")
    config.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"))
    config.add_argument("--service-tier", choices=("standard", "priority"))
    config.add_argument("--base-url")
    config.add_argument("--api-key-env")
    config.add_argument("--codex-bin")
    args = parser.parse_args()
    root = args.data_dir or data_root()
    # Stdio MCP must preserve Unicode and reserve stdout solely for JSON-RPC.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    from mogoo_codex_next.config import load_config, public_config, save_config
    if args.command == "hook":
        try:
            if not os.environ.get("MOGOO_CODEX_NEXT_WORKER"):
                raw = sys.stdin.read(100_000)
                event = json.loads(raw)
                if isinstance(event, dict) and event.get("hook_event_name") == "Stop" and not event.get("stop_hook_active"):
                    from mogoo_codex_next.service import NextService
                    service = NextService(root)
                    try:
                        service.record_stop(event)
                    finally:
                        service.shutdown()
        except Exception:
            # Optional acceleration. Panel reads completed turns authoritatively.
            pass
        print("{}")
        return 0
    if args.command == "serve":
        from mogoo_codex_next.server import McpServer
        from mogoo_codex_next.service import NextService
        McpServer(NextService(root), PROJECT_ROOT).run()
        return 0
    if args.command == "configure":
        changes = {key: getattr(args, key) for key in
                   ("provider", "model", "reasoning_effort", "service_tier", "base_url", "api_key_env", "codex_bin")
                   if getattr(args, key) is not None}
        print(json.dumps({"config": public_config(save_config(root, changes))}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "status":
        print(json.dumps({"data_dir": str(root), "config": public_config(load_config(root))}, ensure_ascii=False, indent=2))
        return 0
    from mogoo_codex_next import __version__
    from mogoo_codex_next.server import RESOURCE_URI, resource_html
    report = {"version": __version__, "python": sys.version.split()[0], "project": str(PROJECT_ROOT),
              "data_dir": str(root), "resource_uri": RESOURCE_URI,
              "ui_ready": bool(resource_html(PROJECT_ROOT)), "config": public_config(load_config(root)),
              "native_panel_verified": False}
    if args.probe_cli:
        from mogoo_codex_next.adapter import CodexAdapter
        adapter = CodexAdapter(load_config(root))
        try:
            report["models"] = adapter.list_models()
            report["cli_protocol_ready"] = True
        except Exception:
            report["cli_protocol_ready"] = False
            report["cli_error"] = "CLI 未就绪，请检查 Codex 安装、登录或配置。"
        finally:
            adapter.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("cli_protocol_ready", True) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as exc:
        # Only configuration errors, never raw inference errors or credentials.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
