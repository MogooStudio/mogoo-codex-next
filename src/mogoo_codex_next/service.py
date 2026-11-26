"""Non-blocking panel orchestration with explicit leases and durable deduplication."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import os
import threading
import time
from typing import Any

from .adapter import AdapterError, CodexAdapter
from .appearance import load_appearance, save_appearance
from .catalog import catalog_config, relay_models
from .config import DEFAULTS, config_fingerprint, is_configured, load_config, public_config, save_config
from .context import snapshot as thread_snapshot
from .store import Store
from .performance import clean_performance
from .update import UpdateManager, ERRORS


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
    "relay_auth": "中转鉴权失败，请检查 API Key 与模型权限后重试。",
    "relay_unsupported": "中转服务未提供此 Responses 接口，请检查 API 地址。",
    "relay_rate_limit": "中转服务请求过多或额度受限，请稍后手动重试。",
    "relay_redirect": "中转接口发生重定向，请核对 API 地址后重试。",
    "relay_http": "中转服务暂时无法完成请求，请稍后手动重试。",
    "relay_timeout": "中转服务响应超时，请手动重试。",
    "relay_network": "无法连接中转服务，请检查网络和代理后重试。",
    "relay_proxy": "HTTP 中转请使用 HTTP(S) 代理设置。",
    "relay_stream": "中转响应提前中断，请手动重试。",
    "relay_failed": "中转请求未能完成，请手动重试。",
    "relay_refusal": "建议模型未能提供建议，请手动重试。",
    "relay_size": "中转返回的结果过大，请手动重试。",
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
        self.updates = UpdateManager(self.root)
        self._lock = threading.RLock()
        self._settings_lock = threading.RLock()
        self._reader_lock = threading.RLock()
        self._reader = None
        self._reader_fp = None
        self._closed = False
        self._pending: set[str] = set()
        self._checking: set[str] = set()
        self._threads: set[str] = set()
        self._jobs: set[str] = set()
        self._verifications: dict[str, list[tuple]] = {}
        self._previews: dict[str, list[tuple]] = {}
        self._config_stamp = None
        self._config_cache = None
        self._public_cache = None
        self._public_stamp = None
        self._probes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="next-probe")
        self._generators = ThreadPoolExecutor(max_workers=2, thread_name_prefix="next-generate")
        self._settings()

    def _settings(self) -> tuple[dict, str]:
        # The lock also orders configure against local probes and poll requests.
        with self._settings_lock:
            try:
                info = (self.root / "config.json").stat()
                stamp = (info.st_mtime_ns, info.st_size, info.st_ino, info.st_ctime_ns)
            except FileNotFoundError:
                stamp = ()
            if self._config_cache is None or stamp != self._config_stamp:
                config = load_config(self.root)
                self._config_cache = (config, config_fingerprint(config))
                self._config_stamp = stamp
                self._public_stamp = None
            config, fingerprint = self._config_cache
            self.store.sync_config(fingerprint, is_configured(config))
            return dict(config), fingerprint

    def _public_config(self, config: dict) -> dict:
        with self._settings_lock:
            # Environment keys can change without touching config.json. Cache
            # only their digest and the public presence/error, never plaintext.
            env_digest = hashlib.sha256(os.environ.get(config.get("api_key_env", ""), "").encode()).hexdigest()
            stamp = (config_fingerprint(config), env_digest)
            if stamp != self._public_stamp:
                self._public_cache = public_config(config)
                self._public_stamp = stamp
            return dict(self._public_cache)

    def _assert_open(self):
        if self._closed:
            raise RuntimeError("插件服务已关闭")

    def _snapshot(self, panel_id: str, thread_id: str, config: dict | None = None) -> dict:
        if config is None:
            config, _ = self._settings()
        result = self.store.snapshot(panel_id, thread_id)
        result["config"] = self._public_config(config)
        result["appearance"] = self.get_appearance()
        result["update"] = self.updates.snapshot(config)
        if not is_configured(config):
            result["status"] = "unconfigured"
            result["suggestions"] = []
        return result

    def open_panel(self, thread_id: str) -> dict:
        self._assert_open()
        config, _ = self._settings()
        self.updates.request_check(config)
        panel_id = self.store.open_panel(thread_id, bool(config.get("auto_generate", True)))
        with self._lock:
            self._threads.add(thread_id)
        if is_configured(config):
            self._schedule(thread_id)
        return self._snapshot(panel_id, thread_id, config)

    def poll(self, panel_id: str, thread_id: str, *, displayed: dict | None = None) -> dict:
        self._assert_open()
        self.store.renew(panel_id, thread_id)
        if displayed is not None:
            self.store.record_display(panel_id, thread_id, displayed["job_id"], displayed["displayed_at"],
                                      stage=displayed.get("stage", "final"))
        config, _ = self._settings()
        if is_configured(config):
            self._schedule(thread_id)
        return self._snapshot(panel_id, thread_id, config)

    def close_panel(self, panel_id: str, thread_id: str) -> dict:
        self.store.close_panel(panel_id, thread_id)
        return {"panel_id": panel_id, "thread_id": thread_id, "status": "paused"}

    def refresh(self, panel_id: str, thread_id: str, request_id: str | None = None) -> dict:
        self._assert_open()
        self.store.renew(panel_id, thread_id)
        config, _ = self._settings()
        if is_configured(config):
            self.store.prepare_refresh(panel_id, thread_id, request_id)
            self._schedule(thread_id)
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
        return self._public_config(config)

    def restore_defaults(self) -> dict:
        # Connection credentials and per-chat auto switches are independent of
        # these user-editable preferences. Reuse the normal atomic save path.
        fields = ("model", "reasoning_effort", "service_tier", "output_mode",
                  "update_check", "dismissed_version")
        return self.configure({field: DEFAULTS[field] for field in fields})

    def get_update(self, *, check=False) -> dict:
        config, _ = self._settings()
        if check:
            self.updates.request_check(config)
        return self.updates.snapshot(config)

    def update_action(self, action: str, panel_id=None, thread_id=None, confirmed=False) -> dict:
        self._assert_open()
        config, _ = self._settings()
        if action == "check":
            self.updates.request_check(config, force=True)
        elif action == "dismiss":
            update = self.updates.snapshot(config)
            target = update.get("target") or update.get("latest")
            if update["state"] in ("auto_update", "manual_update") and target:
                self.configure({"dismissed_version": target})
        elif action == "install":
            if confirmed is not True or not panel_id or not thread_id:
                raise ValueError("请在面板确认更新命令后再执行。")
            self.store.renew(panel_id, thread_id)  # Verify live, host-bound ownership.
            code = self.updates.start_install()
            if code:
                raise ValueError(ERRORS[code])
        elif action != "status":
            raise ValueError("更新操作无效")
        return {"update": self.get_update(), "config": self.get_settings()}

    def get_appearance(self) -> dict:
        return load_appearance(self.root)

    def set_appearance(self, skin: str) -> dict:
        # No config fingerprint changes, job invalidation, or inference scheduling.
        self._assert_open()
        return save_appearance(self.root, skin)

    def configure(self, changes: dict) -> dict:
        self._assert_open()
        with self._settings_lock:
            _, old_fingerprint = self._settings()
            config = save_config(self.root, changes)
            self._config_cache = None
            self._public_stamp = None
            self.store.sync_config(config_fingerprint(config), is_configured(config))
        if is_configured(config) and config_fingerprint(config) != old_fingerprint:
            with self._lock:
                threads = list(self._threads)
            for thread_id in threads:
                if self.store.has_live_panel(thread_id):
                    self._schedule(thread_id)
        return self._public_config(config)

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
            source = {key: saved[key] for key in ("provider", "base_url")}
        config = catalog_config(source, saved)
        return relay_models(config)

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

    def _schedule(self, thread_id: str):
        with self._lock:
            if self._closed:
                return
            self._threads.add(thread_id)
            self._pending.add(thread_id)
            if thread_id in self._checking:
                return
            self._checking.add(thread_id)
            self._probes.submit(self._check_loop, thread_id)

    def _check_loop(self, thread_id: str):
        with self._lock:
            self._pending.discard(thread_id)
            verifications = self._verifications.pop(thread_id, [])
            previews = self._previews.pop(thread_id, [])
        try:
            if not self._closed:
                self._check(thread_id, verifications, previews)
        finally:
            with self._lock:
                # Each dispatch reads at most once, then yields its FIFO slot.
                # Keep the flag while enqueuing so a racing poll cannot lose work
                # or insert a duplicate dispatch for this thread.
                if not self._closed and (thread_id in self._pending or thread_id in self._verifications or thread_id in self._previews):
                    self._probes.submit(self._check_loop, thread_id)
                else:
                    self._checking.discard(thread_id)

    def _verify_later(self, job, suggestions, usage, error):
        self.store.record_result(job["id"], usage, error, performance=job.get("_performance"), suggestions=suggestions)
        with self._lock:
            if self._closed:
                self._finish_job(job, suggestions, usage, error=error, verified=False)
                self._jobs.discard(job["id"])
                return
            self._verifications.setdefault(job["thread_id"], []).append((job, suggestions, usage, error))
            self._schedule(job["thread_id"])

    def _preview_later(self, job, suggestion, performance):
        first = _suggestions([suggestion])
        with self._lock:
            if self._closed or job["id"] not in self._jobs:
                return
            self._previews.setdefault(job["thread_id"], []).append(
                (job, first, clean_performance(performance), time.perf_counter()))
            self._schedule(job["thread_id"])

    def _finish_job(self, job, suggestions, usage, *, error=None, verified=True, read_started=None, read_finished=None):
        performance = dict(job.get("_performance", {}))
        returned = job.get("_returned_clock")
        if returned is not None:
            performance["verification_ms"] = (time.perf_counter() - returned) * 1000
            if read_started is not None and read_finished is not None:
                performance["verification_queue_ms"] = max(0, read_started - returned) * 1000
                performance["verification_read_ms"] = (read_finished - read_started) * 1000
        return self.store.finish_job(job["id"], suggestions, usage, error=error, verified=verified, performance=performance)

    def _read_observe(self, thread_id: str, config: dict, fingerprint: str) -> bool:
        # Serialize both RPC and publication; start timestamps protect against an
        # older observation arriving from another MCP process after a newer one.
        with self._reader_lock:
            observed_at = self.store.clock()
            thread = self._read_adapter(config).read_thread(thread_id)
            context = thread_snapshot(thread)
            return self.store.observe(thread_id, context, fingerprint, observed_at=observed_at)

    def _check(self, thread_id: str, verifications: list, previews=()):
        fingerprint = ""
        read_started = read_finished = None
        try:
            config, fingerprint = self._settings()
            if self._closed or not is_configured(config) or not self.store.has_live_panel(thread_id):
                return
            read_started = time.perf_counter()
            try:
                observed = self._read_observe(thread_id, config, fingerprint)
            finally:
                read_finished = time.perf_counter()
            if not observed:
                return
            current, current_fp = self._settings()
            if self._closed or fingerprint != current_fp or not is_configured(current):
                return
            # Publish completed calls before considering pending user intents;
            # clicks made while that call was active were already attached.
            for old_job, suggestions, usage, error in verifications:
                self._finish_job(old_job, suggestions, usage, error=error,
                                 verified=old_job["config_fp"] == fingerprint,
                                 read_started=read_started, read_finished=read_finished)
                with self._lock:
                    self._jobs.discard(old_job["id"])
            verifications.clear()
            for preview_job, first, performance, received in previews:
                if preview_job["config_fp"] == fingerprint:
                    performance["preview_verification_ms"] = (time.perf_counter() - received) * 1000
                    self.store.publish_preview(preview_job["id"], first, performance)
            job = None
            intents = self.store.pending_refreshes(thread_id)
            for intent in intents:
                try:
                    candidate = self.store.queue_job(thread_id, fingerprint, manual=True,
                                                     panel_id=intent["panel_id"], request_id=intent["id"])
                except ValueError:
                    continue  # The initiating panel may have closed during the read.
                job = job or candidate
            if job is None and not intents:
                job = self.store.queue_job(thread_id, fingerprint)
            # If a click arrived after this read began, keep its durable intent;
            # the next host poll will request another bounded read.
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
                self.store.read_error(thread_id, fingerprint, "无法读取当前聊天，请更新或重开 Codex 后重试。")
        finally:
            for old_job, suggestions, usage, error in verifications:
                self._finish_job(old_job, suggestions, usage, error=error, verified=False,
                                 read_started=read_started, read_finished=read_finished)
                with self._lock:
                    self._jobs.discard(old_job["id"])

    def _generate(self, job: dict, config: dict):
        adapter = None
        usage: dict = {}
        suggestions: list = []
        error = None
        invoked = False
        deferred = False
        try:
            if self._closed:
                self.store.cancel_job(job["id"])
                return
            if not self.store.begin_job(job["id"]):
                return
            current, fingerprint = self._settings()
            if self._closed or fingerprint != job["config_fp"] or not is_configured(current):
                self.store.cancel_job(job["id"])
                return
            adapter = self.adapter_factory(dict(config))
            if self._closed:
                self.store.cancel_job(job["id"])
                return
            if not self.store.mark_invoked(job["id"]):
                return
            invoked = True
            started = time.perf_counter()
            performance = {}
            try:
                result = adapter.generate(job["context"], on_first=lambda item, timing: self._preview_later(job, item, timing))
                performance = clean_performance(result.get("performance"))
                usage = _usage(result.get("usage", {}))
                suggestions = _suggestions(result.get("suggestions", []))
            except Exception as exc:
                performance = clean_performance(getattr(exc, "performance", None)) or performance
                usage = _usage(getattr(exc, "usage", {})) or usage
                error = _generation_error(exc)
            job["_returned_clock"] = time.perf_counter()
            performance["generation_ms"] = (job["_returned_clock"] - started) * 1000
            job["_performance"] = performance
            # Billing evidence is saved even if this read fails, the lease expired,
            # a new turn started, or settings changed while the request was running.
            self._verify_later(job, suggestions, usage, error)
            deferred = True
        except Exception as exc:
            if invoked:
                self._finish_job(job, [], _usage(getattr(exc, "usage", {})) or usage,
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
                if not deferred:
                    self._jobs.discard(job["id"])

    def shutdown(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending.clear()
            self._previews.clear()
            pending_results = [item for values in self._verifications.values() for item in values]
            self._verifications.clear()
            jobs = list(self._jobs)
        for job_id in jobs:
            self.store.cancel_job(job_id)
        for job, suggestions, usage, error in pending_results:
            self._finish_job(job, suggestions, usage, error=error, verified=False)
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
