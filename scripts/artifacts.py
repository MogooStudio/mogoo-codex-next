"""Shared, explicit public artifact allowlist; private acceptance records stay out."""
from pathlib import Path
import re
import shutil
from urllib.parse import unquote, urlsplit

INCLUDE = ("plugin.json", "mcp.json", ".mcp.json", ".codex-plugin/plugin.json",
           ".agents/plugins/marketplace.json", "hooks", "skills", "src", "ui",
           "scripts/run.py", "scripts/launch", "scripts/launch.exe", "scripts/launcher_windows.go",
           "scripts/build_launcher.py", "scripts/package.py", "scripts/artifacts.py", "pyproject.toml",
           "LICENSE", "THIRD_PARTY.md", "README.md", "README.zh-CN.md", "CHANGELOG.zh.md")
README_FILES = ("README.md", "README.zh-CN.md")


def relative_links(text: str):
    # Inline links/images and reference definitions; ignore external URLs/anchors.
    links = re.findall(r'!?\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)', text)
    links += re.findall(r'^\s*\[[^\]]+\]:\s*(<[^>]+>|\S+)', text, re.M)
    for value in links:
        value = value.strip('<>')
        parts = urlsplit(value)
        if parts.scheme or parts.netloc or not parts.path:
            continue
        yield unquote(parts.path)


def artifact_files(root: Path) -> list[Path]:
    root = root.resolve()
    names = list(INCLUDE)
    for readme in README_FILES:
        for link in relative_links((root / readme).read_text(encoding="utf8")):
            if link.startswith("docs/images/"):
                names.append(link)
    files = set()
    for name in names:
        source = root / name
        if not source.exists():
            raise ValueError("发布文件缺失：" + name)
        candidates = source.rglob("*") if source.is_dir() else [source]
        for path in candidates:
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("发布目录不允许符号链接：" + str(relative))
            if path.is_file():
                files.add(relative)
    return sorted(files)


def stage(root: Path, destination: Path) -> list[str]:
    if destination.exists():
        raise ValueError("发布暂存目录已存在")
    files = artifact_files(root)
    destination.mkdir(parents=True)
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    (destination / "scripts/launch").chmod(0o755)
    return [p.as_posix() for p in files]


def check_readme_links(root: Path):
    root = root.resolve()
    for readme in README_FILES:
        for link in relative_links((root / readme).read_text(encoding="utf8")):
            target = (root / link).resolve()
            if not target.is_relative_to(root) or not target.exists():
                raise ValueError("README 相对链接不可达：" + readme + " → " + link)


# Patterns require a payload so their own source and credential format names are safe.
SECRET_PATTERNS = {
    "key": re.compile(r"\b(?:sk-|sk_|gsk_|hf_|ghp_|github_pat_|glpat-|xox[baprs]-|AKIA|AIza)[A-Za-z0-9_-]{12,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "credential_literal": re.compile(r'''(?:api[_-]?key|access[_-]?token|password|secret)["']?\s*[:=]\s*["'](?P<payload>[A-Za-z0-9_./+=-]{20,})["']''', re.I),
    "protected": re.compile(r"dpapi:[A-Za-z0-9+/=]{16,}"),
    "bearer": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.I),
    "local_path": re.compile(r"(?:[A-Za-z]:[\\/]Users[\\/]|/Users/|/home/)[A-Za-z0-9_.-]+", re.I),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
ALLOWED_VALUES = {"release@noreply.invalid"}


def scan_public(root: Path) -> list[str]:
    """Report file/line/category only; never print matched sensitive text."""
    findings = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # Decode binary too: accidental embedded credentials are still a leak.
        text = path.read_bytes().decode("utf8", "replace")
        for category, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                payload = match.groupdict().get("payload", match[0])
                if payload in ALLOWED_VALUES or payload.startswith("synthetic-"):
                    continue
                # Explicit synthetic placeholders are allowed, including sk-synthetic-….
                if match[0].startswith(("sk-synthetic-", "Bearer synthetic-", "dpapi:synthetic-")) and category in ("key", "bearer", "protected"):
                    continue
                findings.append(f"{path.relative_to(root).as_posix()}:{text.count(chr(10), 0, match.start()) + 1}:{category}")
    return findings
