#!/usr/bin/env python3
"""Build a portable MCP bundle with a local legacy compatibility entrypoint."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
INCLUDE = ("plugin.json", "mcp.json", ".mcp.json", ".codex-plugin", ".agents", "hooks",
           "skills", "src", "ui", "scripts/run.py", "LICENSE", "THIRD_PARTY.md",
           "README.md", "README.zh-CN.md", "CHANGELOG.zh.md", "docs")


def build(destination: Path) -> Path:
    destination = destination.resolve()
    archive = destination.parent / (destination.name + ".zip")
    if destination.exists() or archive.exists():
        raise ValueError("输出目录已存在，请指定新的目录，避免覆盖已有文件。")
    destination.mkdir(parents=True)
    for name in INCLUDE:
        source = ROOT / name
        target = destination / name
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # Portable mcp.json only permits a bare executable or contained ./ path.
    # Keep its authored command; only the legacy format supports this local path.
    path = destination / ".mcp.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mcpServers"]["mogoo-codex-next"]["command"] = sys.executable
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                stream.write(path, Path(destination.name) / path.relative_to(destination))
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包当前机器可安装的插件；不安装、不修改 Codex 配置")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/mogoo-codex-next-local")
    args = parser.parse_args()
    try:
        print(build(args.output))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
