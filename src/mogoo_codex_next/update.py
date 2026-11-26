"""Bounded public tag discovery and explicit, app-only plugin updates."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlsplit

from . import __version__
from .adapter import find_codex, process_options, worker_env
from .release_channel import (INSTALL_COMMAND, MARKETPLACE, MARKETPLACE_REF,
                              MARKETPLACE_REPOSITORY, PLUGIN, PUBLIC_REPOSITORY, RELEASE_REF,
                              UPDATE_COMMANDS)

CHECK_INTERVAL = 6 * 3600
FAILURE_INTERVAL = 3600
NETWORK_TIMEOUT = 10
MAX_REFS_BYTES = 1024 * 1024
STEP_TIMEOUT = 60
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
ERRORS = {
    "source": "当前安装来源不支持一键更新，请使用公开市场的手动安装命令。",
    "busy": "另一个面板正在更新，请等待完成。",
    "runtime": "未找到可用的 Codex 运行组件，请使用手动命令。",
    "timeout": "更新命令超时，已停止本次更新。请检查网络后使用手动命令。",
    "permission": "安装文件被占用或无写入权限。请关闭使用该插件的聊天，再在外部终端运行手动命令。",
    "marketplace": "公开市场更新失败，请检查 Git、网络和市场来源后使用手动命令。",
    "install": "插件安装失败，当前会话仍使用原版本。请在外部终端运行手动命令。",
    "interrupted": "上次更新被中断，请在外部终端确认插件状态并运行手动命令。",
    "local": "无法保存更新状态，请检查插件数据目录的写入权限后使用手动命令。",
    "version": "公开市场中的版本尚未就绪，本次未安装。请稍后检查或使用手动命令。",
    "concurrent": "Codex 可能正在更新插件。请稍后查看已安装版本；本次不会自动重试。",
}


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or len(value) > 64 or not _VERSION.fullmatch(value):
        raise ValueError("版本格式无效")
    return tuple(map(int, value.split(".")))


def refs_url(repository: str) -> str:
    parts = urlsplit(repository)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or parts.port not in (None, 443)
            or any(c.isspace() or ord(c) < 32 for c in repository)):
        raise ValueError("更新地址必须为 HTTPS Git 仓库")
    return repository.rstrip("/") + "/info/refs?service=git-upload-pack"


def parse_refs(data: bytes) -> list[str]:
    """Parse Git v0/v1 pkt-lines; malformed framing invalidates the response."""
    if not isinstance(data, bytes) or len(data) > MAX_REFS_BYTES:
        raise ValueError("标签列表过大")
    if not data or not data.endswith(b"0000"):
        raise ValueError("标签列表不完整")
    offset = 0
    versions = set()
    while offset < len(data):
        header = data[offset:offset + 4]
        if len(header) != 4 or not re.fullmatch(b"[0-9a-fA-F]{4}", header):
            raise ValueError("标签列表格式无效")
        length = int(header, 16)
        offset += 4
        if length in (0, 1, 2):
            continue
        if length < 4 or length > 65520 or offset + length - 4 > len(data):
            raise ValueError("标签列表不完整")
        line = data[offset:offset + length - 4].split(b"\0", 1)[0].rstrip(b"\n")
        offset += length - 4
        match = re.fullmatch(rb"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64}) refs/tags/v([0-9]+\.[0-9]+\.[0-9]+)", line)
        if match:
            value = match[1].decode("ascii")
            try:
                version_tuple(value)
            except ValueError:
                continue
            versions.add(value)
    return sorted(versions, key=version_tuple)


def fetch_versions(repository: str = PUBLIC_REPOSITORY) -> list[str]:
    # Reuse the relay's explicit/system proxy selection and redirect refusal.
    from .relay import _opener, _read_budget
    request = urllib.request.Request(refs_url(repository), headers={
        "Accept": "application/x-git-upload-pack-advertisement",
        "User-Agent": PLUGIN + "/" + __version__,
    })
    deadline = time.monotonic() + NETWORK_TIMEOUT
    with _opener().open(request, timeout=NETWORK_TIMEOUT) as response:
        if response.status != 200:
            raise ValueError("更新源不可用")
        with _read_budget(response, deadline):
            data = response.read(MAX_REFS_BYTES + 1)
        if time.monotonic() > deadline:
            raise TimeoutError()
    return parse_refs(data)


@contextmanager
def file_lock(path: Path):
    """OS-owned locks survive no crashes; keep the inode to prevent unlink races."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                stream.write(b"\0"); stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired and os.name == "nt":
            import msvcrt
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def _read_json(path: Path) -> dict:
    try:
        if path.stat().st_size > 16384:
            return {}
        value = json.loads(path.read_text(encoding="utf8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".update-")
    try:
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def installed_from_market(project: Path, codex_home: Path | None = None) -> bool:
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    base = (home / "plugins/cache" / MARKETPLACE / PLUGIN).resolve()
    project = project.resolve()
    try:
        relative = project.relative_to(base)
        return len(relative.parts) == 1 and manifest_version(project / ".codex-plugin/plugin.json") == relative.parts[0]
    except (ValueError, OSError):
        return False


def public_market_commands(home: Path) -> tuple[tuple[str, ...], ...]:
    """Select the update commands for the registered public marketplace.

    Conservative support for the CLI's generated TOML, including Python 3.10.
    Unknown/ambiguous configurations keep manual commands available.
    """
    try:
        path = home / "config.toml"
        if path.stat().st_size > 1024 * 1024:
            return ()
        text = path.read_text(encoding="utf8")
        market = re.escape(MARKETPLACE)
        section = rf"""^\[marketplaces\.(?:{market}|"{market}"|'{market}')\]\s*\n([^\[]*)"""
        sections = re.findall(section, text, re.M)
        if len(sections) != 1:
            return ()
        fields = {}
        for key, value in re.findall(r"^\s*(source|source_type|ref)\s*=\s*([^\n]+)", sections[0], re.M):
            value = value.strip()
            if key in fields:
                return ()
            fields[key] = json.loads(value) if value.startswith('"') else value[1:-1] if value.startswith("'") and value.endswith("'") else None
    except (OSError, ValueError):
        return ()
    if fields.get("source_type") != "git" or fields.get("ref", MARKETPLACE_REF) != MARKETPLACE_REF:
        return ()
    if fields.get("source") != MARKETPLACE_REPOSITORY:
        return ()

    catalog = _read_json(home / ".tmp/marketplaces" / MARKETPLACE / ".agents/plugins/marketplace.json")
    entries = catalog.get("plugins")
    if catalog.get("name") != MARKETPLACE or not isinstance(entries, list):
        return ()
    sources = [entry.get("source") for entry in entries if isinstance(entry, dict) and entry.get("name") == PLUGIN]
    if sources == [{"source": "url", "url": PUBLIC_REPOSITORY, "ref": RELEASE_REF}]:
        return (INSTALL_COMMAND,)
    return ()


def manifest_version(path: Path) -> str | None:
    descriptor = _read_json(path)
    value = descriptor.get("version")
    if descriptor.get("name") != PLUGIN:
        return None
    try:
        version_tuple(value)
    except ValueError:
        return None
    return value


def local_versions(home: Path) -> dict:
    """Read host-owned caches without networking, config writes or lock probes."""
    base = home / "plugins/cache" / MARKETPLACE / PLUGIN
    versions = []
    try:
        for path in base.iterdir():
            if not path.is_dir() or path.is_symlink() or path.resolve().parent != base.resolve():
                continue
            value = manifest_version(path / ".codex-plugin/plugin.json")
            if value is not None and value == path.name:
                versions.append(value)
    except OSError:
        pass
    return {"installed": max(versions, key=version_tuple) if versions else None,
            "snapshot": manifest_version(home / ".tmp/marketplaces" / MARKETPLACE / "plugin.json")}


def highest_version(*values: str | None) -> str | None:
    return max((value for value in values if value is not None), key=version_tuple, default=None)


def update_state(running: str, installed: str | None, latest: str | None, status: str, public: bool,
                 snapshot: str | None = None) -> str:
    if installed and version_tuple(installed) > version_tuple(running):
        return "pending_restart"
    if status == "unavailable":
        return "unavailable"
    target = highest_version(latest, snapshot)
    floor = highest_version(installed, running)
    if status == "ok" and target and version_tuple(target) > version_tuple(floor):
        return "auto_update" if public else "manual_update"
    return "current"


def _hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def classify_failure(step: int, text: str) -> str:
    value = text.lower()
    if any(word in value for word in ("access is denied", "permission denied", "os error 5", "os error 32", "being used", "sharing violation")):
        return "permission"
    return "marketplace" if step == 0 else "install"


def _run_step(arguments: list[str]) -> tuple[int, str]:
    """Bound the CLI and its Git subprocesses; never let a timed-out clone continue."""
    with tempfile.TemporaryFile() as output:
        options = process_options()
        if os.name != "nt":
            options["start_new_session"] = True
        proc = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                env=worker_env(), **options)
        try:
            proc.wait(timeout=STEP_TIMEOUT)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                taskkill = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe")
                try:
                    subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, **process_options())
                except (OSError, subprocess.SubprocessError):
                    pass
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            raise
        output.seek(0)
        return proc.returncode, output.read(16384).decode("utf8", "replace")


class UpdateManager:
    def __init__(self, root: Path, project: Path | None = None):
        self.root = Path(root)
        self.project = project or Path(__file__).resolve().parents[2]
        self.home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        # Codex may delete this process's original cache directory later.
        self._from_market = installed_from_market(self.project, self.home)
        self._hooks_before = _hash(self.project / "hooks/hooks.json")
        self._mutex = threading.Lock()
        self._checking = False

    def _cache(self):
        cache = _read_json(self.root / "update.json")
        stamp = cache.get("checked_at")
        if type(stamp) not in (float, int) or not math.isfinite(stamp) or not 0 <= stamp <= time.time() + 60:
            return {}
        if cache.get("status") not in ("ok", "unavailable"):
            return {}
        try:
            if cache.get("latest") is not None:
                version_tuple(cache["latest"])
        except ValueError:
            return {}
        return {"latest": cache.get("latest"), "status": cache["status"], "checked_at": stamp}

    def snapshot(self, config: dict) -> dict:
        cache = self._cache()
        latest = cache.get("latest")
        enabled = config.get("update_check", True)
        local = local_versions(self.home)
        target = highest_version(__version__, latest, local["installed"], local["snapshot"])
        latest_stale = bool(latest and version_tuple(latest) < version_tuple(target))
        commands = public_market_commands(self.home)
        repository = PUBLIC_REPOSITORY
        public = self._from_market and bool(commands)
        status = cache.get("status", "unavailable") if enabled else "disabled"
        state = update_state(__version__, local["installed"], latest, status, public, local["snapshot"])
        dismissed = state in ("auto_update", "manual_update") and config.get("dismissed_version") == target
        # Additive wire contract: already-open historical panels still read
        # current/available and cannot reload their JS when the backend changes.
        result = {"running": __version__, "current": __version__, **local, "latest": latest, "state": state,
                  "target": target, "latest_stale": latest_stale,
                  "available": state == "pending_restart" or (state in ("auto_update", "manual_update") and not dismissed),
                  "checked_at": cache.get("checked_at"), "status": status,
                  "checking": self._checking, "dismissed": dismissed,
                  "can_install": public and state == "auto_update" and not dismissed,
                  "commands": ["codex " + " ".join(command) for command in (commands or UPDATE_COMMANDS)],
                  "changelog_url": repository.removesuffix(".git") + f"/blob/{RELEASE_REF}/CHANGELOG.zh.md",
                  "operation": {}}
        operation = _read_json(self.root / "update-operation.json")
        if operation.get("state") in ("running", "success", "error"):
            if operation["state"] == "running":
                try:
                    with file_lock(self.root / "update-install.lock") as acquired:
                        if acquired:
                            operation = {"state": "error", "code": "interrupted"}
                except OSError:
                    operation = {"state": "error", "code": "local"}
            # Only fixed messages and known fields ever reach the panel.
            code = operation.get("code") if isinstance(operation.get("code"), str) else ""
            result["operation"] = {"state": operation["state"], "step": operation.get("step") if operation.get("step") in (0, 1, 2) else 0,
                                   "code": code if code in ERRORS else "", "message": ERRORS.get(code, ""),
                                   "hooks_changed": operation.get("hooks_changed") is True}
        return result

    def request_check(self, config: dict, *, force: bool = False) -> bool:
        if not force and not config.get("update_check", True):
            return False
        with self._mutex:
            if self._checking:
                return False
            cache = self._cache()
            interval = FAILURE_INTERVAL if cache.get("status") == "unavailable" else CHECK_INTERVAL
            if (not force or cache.get("status") == "unavailable") and time.time() - cache.get("checked_at", 0) < interval:
                return False
            self._checking = True
        threading.Thread(target=self._check, args=(force,), daemon=True, name="next-update-check").start()
        return True

    def _check(self, force):
        try:
            with file_lock(self.root / "update-check.lock") as acquired:
                if not acquired:
                    return
                cache = self._cache()
                interval = FAILURE_INTERVAL if cache.get("status") == "unavailable" else CHECK_INTERVAL
                if (not force or cache.get("status") == "unavailable") and time.time() - cache.get("checked_at", 0) < interval:
                    return
                try:
                    commands = public_market_commands(self.home)
                    repository = PUBLIC_REPOSITORY
                    versions = fetch_versions(repository)
                    cache = {"latest": versions[-1] if versions else None, "status": "ok", "checked_at": time.time()}
                except Exception:
                    cache = {"latest": cache.get("latest"), "status": "unavailable", "checked_at": time.time()}
                _write_json(self.root / "update.json", cache)
        except OSError:
            pass
        finally:
            with self._mutex:
                self._checking = False

    def start_install(self) -> str | None:
        if not self._from_market or not public_market_commands(self.home):
            return "source"
        local = local_versions(self.home)
        target = highest_version(__version__, self._cache().get("latest"), local["snapshot"], local["installed"])
        installed = local["installed"]
        if version_tuple(target) <= version_tuple(__version__) and (not installed or version_tuple(installed) < version_tuple(target)):
            return "version"
        ready = threading.Event()
        outcome = []
        threading.Thread(target=self._install, args=(ready, outcome), daemon=True, name="next-update-install").start()
        ready.wait(2)
        return outcome[0] if outcome else "local"

    def _install(self, ready, outcome):
        try:
            with file_lock(self.root / "update-install.lock") as acquired:
                if not acquired:
                    outcome.append("busy"); ready.set(); return
                commands = public_market_commands(self.home)
                if not commands:
                    outcome.append("source"); ready.set(); return
                first_step = 1
                _write_json(self.root / "update-operation.json", {"state": "running", "step": first_step})
                outcome.append(None); ready.set()
                code = None
                binary = None
                local = local_versions(self.home)
                latest_version = highest_version(__version__, self._cache().get("latest"), local["snapshot"], local["installed"])
                def installed():
                    value = local_versions(self.home)["installed"]
                    return bool(value and latest_version and version_tuple(value) >= version_tuple(latest_version))

                if not latest_version:
                    code = "version"
                if code is None and not installed():
                    for step, command in enumerate(commands, start=first_step):
                        # Host staging locks cover neither the entire upgrade
                        # nor cache activation. Reconcile host-owned state at
                        # every boundary instead of probing those lock files.
                        if installed():
                            break
                        if public_market_commands(self.home) != commands:
                            code = "source"; break
                        if binary is None:
                            try:
                                binary = find_codex()
                            except Exception:
                                code = "runtime"; break
                        _write_json(self.root / "update-operation.json", {"state": "running", "step": step})
                        try:
                            returncode, text = _run_step([binary, *command])
                            if returncode:
                                code = classify_failure(step, text)
                        except subprocess.TimeoutExpired:
                            code = "timeout"
                        except OSError:
                            code = "runtime"
                        # Codex may have activated the target even if our CLI
                        # lost a race or failed while removing obsolete files.
                        if installed():
                            code = None; break
                        if code:
                            break
                if code is None and not installed():
                    code = "concurrent"
                current = local_versions(self.home)["installed"]
                after = _hash(self.home / "plugins/cache" / MARKETPLACE / PLUGIN / current / "hooks/hooks.json") if current else None
                changed = bool(self._hooks_before and after and after != self._hooks_before)
                _write_json(self.root / "update-operation.json", {"state": "error" if code else "success", "code": code,
                                                               "step": 2, "hooks_changed": changed})
        except Exception:
            try:
                _write_json(self.root / "update-operation.json", {"state": "error", "code": "local"})
            except OSError:
                pass
            if not ready.is_set():
                outcome.append("local"); ready.set()
