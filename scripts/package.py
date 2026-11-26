#!/usr/bin/env python3
"""Build a portable MCP bundle with a local legacy compatibility entrypoint."""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from artifacts import INCLUDE, stage

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path) -> Path:
    destination = destination.resolve()
    archive = destination.parent / (destination.name + ".zip")
    if destination.exists() or archive.exists():
        raise ValueError("输出目录已存在，请指定新的目录，避免覆盖已有文件。")
    stage(ROOT, destination)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                name = Path(destination.name) / path.relative_to(destination)
                if path == destination / "scripts/launch":
                    entry = zipfile.ZipInfo.from_file(path, name)
                    entry.create_system = 3
                    entry.external_attr = 0o100755 << 16
                    stream.writestr(entry, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
                else:
                    stream.write(path, name)
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="打包当前机器可安装的插件；不安装、不修改 Codex 配置")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/mogoo-codex-next-local")
    args = parser.parse_args()
    try:
        print(build(args.output))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
