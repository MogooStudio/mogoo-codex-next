"""Non-blocking panel orchestration with explicit leases and durable deduplication."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from typing import Any

from .adapter import AdapterError, CodexAdapter
from .appearance import load_appearance, save_appearance
from .catalog import catalog_config, relay_models
from .config import config_fingerprint, is_configured, load_config, public_config, save_config
from .context import snapshot as thread_snapshot
from .store import Store


_GENERATION_ERROR_MESSAGES = {
    "relay_key": "中转 API Key 不可用，请在建议设置中重新填写并保存。",
    "process_failed": "建议模型进程未成功完成，请检查登录、额度或网络后手动重试。",
    "output_missing": "建议模型未返回结果，请手动重试。",
    "tool_attempt": "建议模型尝试调用工具，本次结果已丢弃，请手动重试。",
    "output_json": "建议模型返回的结果无法解析，请手动重试。",
    "output_schema": "建议模型返回的建议不符合格式或长度要求，请手动重试。",
    "process_start": "无法启动建议模型进程，请检查 Codex CLI 后重试。",
    "output_read": "无法读取建议模型结果，请手动重试。",
    "local_io": "无法准备建议模型的临时文件，请检查本机临时目录后重试。",
    "timeout": "建议模型调用超过 60 秒，请手动重试。",
}
_DEFAULT_GENERATION_ERROR = "生成建议失败，请检查独立模型配置后手动刷新。"


def _generation_error(exc: Exception) -> str:
    # Provider stderr and arbitrary exception text must never reach the panel.
    if isinstance(exc, AdapterError) and isinstance(exc.code, str):
        return _GENERATION_ERROR_MESSAGES.get(exc.code, _DEFAULT_GENERATION_ERROR)
    return _DEFAULT_GENERATION_ERROR


def _usage(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    return {str(key): number for key, number in value.items()
            if isinstance(number, int) and not isinstance(number, bool) and number >= 0}


def _suggestions(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("建议格式无效")
    result = []
    for item in value[:3]:
        if not isinstance(item, dict) or not isinstance(item.get("prompt"), str) or not item["prompt"].strip():
            raise ValueError("建议格式无效")
        result.append({"title": str(item.get("title") or "下一步")[:100],
                       "prompt": item["prompt"].strip()[:12000],
                       "reason": str(item.get("reason") or "")[:1000]})
    return result


class NextService:
    def __init__(self, root: Path, adapter_factory=CodexAdapter):
        self.root = Path(root)
        self.store = Store(self.root)
        self.adapter_factory = adapter_factory
        self._lock = threading.RLock()
        self._settings_lock = threading.RLock()
        self._reader_lock = threading.RLock()
        self._reader = None
        self._reader_fp = None
        self._closed = False
        self._pending: dict[str, set[str | None]] = {}
        self._checking: set[str] = set()
        self._threads: set[str] = set()
        self._jobs: set[str] = set()
        self._probes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="next-probe")
        self._generators = ThreadPoolExecutor(max_workers=2, thread_name_prefix="next-generate")
        self._settings()

    def _settings(self) -> tuple[dict, str]:
        # The lock also orders configure against local probes and poll requests.
        with self._settings_lock:
            config = load_config(self.root)
            fingerprint = config_fingerprint(config)
            self.store.sync_config(fingerprint, is_configured(config))
            return config, fingerprint

    def _assert_open(self):
        if self._closed:
            raise RuntimeError("插件服务已关闭")

    def _snapshot(self, panel_id: str, thread_id: str, config: dict | None = None) -> dict:
        if config is None:
            config, _ = self._settings()
        result = self.store.snapshot(panel_id, thread_id)
        result["config"] = public_config(config)
        result["appearance"] = self.get_appearance()
        if not is_configured(config):
            result["status"] = "unconfigured"
            result["suggestions"] = []
        return result

    def open_panel(self, thread_id: str) -> dict:
        self._assert_open()
        config, _ = self._settings()
        panel_id = self.store.open_panel(thread_id, bool(config.get("auto_generate", True)))
        with self._lock:
            self._threads.add(thread_id)
        if is_configured(config):
            self._schedule(thread_id)
        return self._snapshot(panel_id, thread_id, config)

    def poll(self, panel_id: str, thread_id: str) -> dict:
        self._assert_open()
        self.store.renew(panel_id, thread_id)
        config, _ = self._settings()
        if is_configured(config):
            self._schedule(thread_id)
        return self._snapshot(panel_id, thread_id, config)

    def close_panel(self, panel_id: str, thread_id: str) -> dict:
        self.store.close_panel(panel_id, thread_id)
        return {"panel_id": panel_id, "thread_id": thread_id, "status": "paused"}

    def refresh(self, panel_id: str, thread_id: str) -> dict:
        self._assert_open()
        self.store.renew(panel_id, thread_id)
        config, _ = self._settings()
        if is_configured(config):
            self.store.prepare_refresh(panel_id, thread_id)
            self._schedule(thread_id, manual_panel=panel_id)
        return self._snapshot(panel_id, thread_id, config)

    def set_auto(self, panel_id: str, thread_id: str, enabled: bool) -> dict:
        self._assert_open()
        if not isinstance(enabled, bool):
            raise ValueError("自动生成开关必须为布尔值")
        self.store.set_auto(panel_id, thread_id, enabled)
        config, _ = self._settings()
        if enabled and is_configured(config):
            self._schedule(thread_id)
        return self._snapshot(panel_id, thread_id, config)

    def get_settings(self) -> dict:
        config, _ = self._settings()
        return public_config(config)

    def get_appearance(self) -> dict:
        return load_appearance(self.root)

    def set_appearance(self, skin: str) -> dict:
        # No config fingerprint changes, job invalidation, or inference scheduling.
        self._assert_open()
        return save_appearance(self.root, skin)

    def configure(self, changes: dict) -> dict:
        self._assert_open()
        with self._settings_lock:
            config = save_config(self.root, changes)
            self.store.sync_config(config_fingerprint(config), is_configured(config))
        if is_configured(config):
            with self._lock:
                threads = list(self._threads)
            for thread_id in threads:
                if self.store.has_live_panel(thread_id):
                    self._schedule(thread_id)
        return public_config(config)

    def _read_adapter(self, config: dict):
        fingerprint = config_fingerprint(config)
        if self._reader is None or self._reader_fp != fingerprint:
            if self._reader is not None:
                self._reader.close()
            self._reader = self.adapter_factory(dict(config))
            self._reader_fp = fingerprint
        return self._reader

    def models(self, source: dict | None = None) -> list:
        self._assert_open()
        saved, _ = self._settings()
        if source is None:
            source = {key: saved[key] for key in ("provider", "base_url", "codex_bin")}
        config = catalog_config(source, saved)
        if config["provider"] == "relay":
            return relay_models(config)
        # A draft CLI path must not replace the reader serving active chats.
        adapter = self.adapter_factory(config)
        try:
            return adapter.list_models()
        finally:
            adapter.close()

    def record_stop(self, event: dict) -> dict:
        # Hook data never supplies context and never authorizes inference.
        if not isinstance(event, dict):
            return {"recorded": False}
        kind = event.get("hook_event_name", event.get("event", "Stop"))
        if kind != "Stop":
            return {"recorded": False}
        thread_id = event.get("session_id") or event.get("thread_id")
        turn_id = event.get("turn_id")
        if not isinstance(thread_id, str) or not thread_id.strip() or len(thread_id) > 512:
            return {"recorded": False}
        self.store.record_stop(thread_id, turn_id if isinstance(turn_id, str) else None)
        return {"recorded": True}

    def _schedule(self, thread_id: str, manual_panel: str | None = None):
        with self._lock:
            if self._closed:
                return
            self._threads.add(thread_id)
            self._pending.setdefault(thread_id, set()).add(manual_panel)
            if thread_id in self._checking:
                return
            self._checking.add(thread_id)
            self._probes.submit(self._check_loop, thread_id)

    def _check_loop(self, thread_id: str):
        while True:
            with self._lock:
                requests = self._pending.pop(thread_id, None)
                if self._closed or requests is None:
                    self._checking.discard(thread_id)
                    return
            try:
                self._check(thread_id, requests)
            except Exception:
                # State I/O failures must not strand the coalescing flag forever.
                pass

    def _read_observe(self, thread_id: str, config: dict, fingerprint: str) -> bool:
        # Serialize both RPC and publication; start timestamps protect against an
        # older observation arriving from another MCP process after a newer one.
        with self._reader_lock:
            observed_at = self.store.clock()
            thread = self._read_adapter(config).read_thread(thread_id)
            context = thread_snapshot(thread)
            return self.store.observe(thread_id, context, fingerprint, observed_at=observed_at)

    def _check(self, thread_id: str, requests: set[str | None]):
        fingerprint = ""
        try:
            config, fingerprint = self._settings()
            if self._closed or not is_configured(config) or not self.store.has_live_panel(thread_id):
                return
            if not self._read_observe(thread_id, config, fingerprint):
                return
            current, current_fp = self._settings()
            if self._closed or fingerprint != current_fp or not is_configured(current):
                return
            job = None
            for panel_id in sorted(value for value in requests if value is not None):
                try:
                    job = self.store.queue_job(thread_id, fingerprint, manual=True, panel_id=panel_id)
                except ValueError:
                    continue  # The initiating panel may have closed during the read.
                if job:
                    break
            if job is None:
                job = self.store.queue_job(thread_id, fingerprint)
            if job:
                with self._lock:
                    if self._closed:
                        self.store.cancel_job(job["id"])
                        return
                    self._jobs.add(job["id"])
                    self._generators.submit(self._generate, job, config)
        except Exception:
            # Do not serialize raw exceptions: providers can include credentials.
            if fingerprint:
                self.store.read_error(thread_id, fingerprint, "无法读取当前聊天，请检查 Codex CLI 后重试。")

    def _generate(self, job: dict, config: dict):
        adapter = None
        usage: dict = {}
        suggestions: list = []
        error = None
        invoked = False
        try:
            if self._closed or not self.store.begin_job(job["id"]):
                self.store.cancel_job(job["id"])
                return
            current, fingerprint = self._settings()
            if self._closed or fingerprint != job["config_fp"] or not is_configured(current):
                self.store.cancel_job(job["id"])
                return
            adapter = self.adapter_factory(dict(config))
            if self._closed or not self.store.mark_invoked(job["id"]):
                self.store.cancel_job(job["id"])
                return
            invoked = True
            try:
                result = adapter.generate(job["context"])
                usage = _usage(result.get("usage", {}))
                suggestions = _suggestions(result.get("suggestions", []))
            except Exception as exc:
                usage = _usage(getattr(exc, "usage", {})) or usage
                error = _generation_error(exc)
            # Billing evidence is saved even if this read fails, the lease expired,
            # a new turn started, or settings changed while the request was running.
            verified = True
            try:
                latest_config, latest_fp = self._settings()
                if latest_fp == job["config_fp"] and self.store.has_live_panel(job["thread_id"]):
                    verified = not self._closed and self._read_observe(job["thread_id"], latest_config, latest_fp)
            except Exception:
                verified = False
            self.store.finish_job(job["id"], suggestions, usage, error=error, verified=verified)
        except Exception as exc:
            if invoked:
                self.store.finish_job(job["id"], [], _usage(getattr(exc, "usage", {})) or usage,
                                      error="生成建议失败，请手动刷新。", verified=False)
            else:
                self.store.cancel_job(job["id"])
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:
                    pass
            with self._lock:
                self._jobs.discard(job["id"])

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            jobs = list(self._jobs)
        for job_id in jobs:
            self.store.cancel_job(job_id)
        self._probes.shutdown(wait=False, cancel_futures=True)
        self._generators.shutdown(wait=False, cancel_futures=True)
        # Never make close/auto wait behind this lock; only process shutdown does.
        with self._reader_lock:
            if self._reader is not None:
                try:
                    self._reader.close()
                except Exception:
                    pass
                self._reader = None
