import copy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mogoo_codex_next.adapter import AdapterError
from mogoo_codex_next.config import save_config
from mogoo_codex_next.service import NextService


def thread_data(thread_id="thread-a", turn_id="turn-1", status="completed"):
    return {"id": thread_id, "name": "测试对话", "turns": [{"id": turn_id, "status": status,
            "items": [{"type": "userMessage", "content": [{"type": "text", "text": "修复页面"}]},
                      {"type": "agentMessage", "phase": "final_answer", "text": "修改完成，可以检查页面。"}]}]}


class FakeBackend:
    def __init__(self):
        self.lock = threading.Lock()
        self.threads = {"thread-a": thread_data(), "thread-b": thread_data("thread-b")}
        self.instances = []
        self.generated = []
        self.reads = 0
        self.read_entered = threading.Event()
        self.generate_entered = threading.Event()
        self.read_gate = threading.Event()
        self.generate_gate = threading.Event()
        self.read_gate.set()
        self.generate_gate.set()
        self.failure = None
        self.read_failure = False

    def factory(self, config):
        adapter = FakeAdapter(self, config)
        with self.lock:
            self.instances.append(adapter)
        return adapter

    def set_thread(self, value):
        with self.lock:
            self.threads[value["id"]] = value


class FakeAdapter:
    def __init__(self, backend, config):
        self.backend = backend
        self.config = config
        self.closed = False
        self.used_for_read = False

    def read_thread(self, thread_id):
        self.used_for_read = True
        self.backend.read_entered.set()
        if not self.backend.read_gate.wait(4):
            raise RuntimeError("fake reader timeout")
        if self.backend.read_failure:
            raise RuntimeError("fake read error: secret-value")
        with self.backend.lock:
            self.backend.reads += 1
            return copy.deepcopy(self.backend.threads[thread_id])

    def list_models(self):
        return [{"id": "fake-model"}]

    def generate(self, context):
        with self.backend.lock:
            self.backend.generated.append((self.config["model"], copy.deepcopy(context)))
        self.backend.generate_entered.set()
        if not self.backend.generate_gate.wait(4):
            raise RuntimeError("fake generation timeout")
        if self.backend.failure:
            raise self.backend.failure
        return {"suggestions": [{"title": "检查效果", "prompt": "请检查刚修改页面的交互。", "reason": "验证本轮修改"}],
                "usage": {"input_tokens": 17, "output_tokens": 5}}

    def close(self):
        self.closed = True


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        save_config(self.root, {"model": "fake-model"})
        self.backend = FakeBackend()
        self.services = []
        self.service = self.make_service()
        self.addCleanup(self.cleanup_services)

    def make_service(self):
        service = NextService(self.root, adapter_factory=self.backend.factory)
        self.services.append(service)
        return service

    def cleanup_services(self):
        self.backend.read_gate.set()
        self.backend.generate_gate.set()
        for service in self.services:
            service.shutdown()
        for service in self.services:
            service._probes.shutdown(wait=True, cancel_futures=True)
            service._generators.shutdown(wait=True, cancel_futures=True)

    def eventually(self, check, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if value := check():
                return value
            time.sleep(0.005)
        self.fail("后台假适配器任务未按时完成")

    def wait_status(self, panel, status, service=None):
        service = service or self.service
        return self.eventually(lambda: (snap if (snap := service.store.snapshot(panel, "thread-a"))["status"] == status else None))

    def test_first_panel_requires_explicit_model_and_never_invokes_adapter(self):
        self.service.configure({"model": ""})
        snap = self.service.open_panel("thread-a")
        self.assertEqual("unconfigured", snap["status"])
        self.assertEqual("", snap["config"]["model"])
        self.service.poll(snap["panel_id"], "thread-a")
        self.service.refresh(snap["panel_id"], "thread-a")
        self.assertEqual([], self.backend.instances)

    def test_poll_and_close_return_while_background_thread_read_is_blocked(self):
        self.backend.read_gate.clear()
        started = time.monotonic()
        snap = self.service.open_panel("thread-a")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(self.backend.read_entered.wait(1))
        started = time.monotonic()
        self.service.poll(snap["panel_id"], "thread-a")
        self.service.close_panel(snap["panel_id"], "thread-a")
        self.assertLess(time.monotonic() - started, 0.5)
        self.backend.read_gate.set()
        self.eventually(lambda: not self.service._checking)
        self.assertEqual([], self.backend.generated)

    def test_stop_is_only_a_marker_and_unbound_ids_are_rejected(self):
        self.assertEqual({"recorded": True}, self.service.record_stop({"session_id": "thread-a", "turn_id": "turn-1"}))
        self.assertEqual({"recorded": False}, self.service.record_stop({"hook_event_name": "PreToolUse", "session_id": "thread-a"}))
        self.assertEqual([], self.backend.instances)
        with self.assertRaises(ValueError):
            self.service.open_panel("")

    def test_completed_reply_generates_once_and_reuses_reader(self):
        panel = self.service.open_panel("thread-a")["panel_id"]
        snap = self.wait_status(panel, "ready")
        self.assertEqual(1, snap["usage"]["calls"])
        self.assertEqual(17, snap["usage"]["input_tokens"])
        for _ in range(5):
            self.service.poll(panel, "thread-a")
        self.eventually(lambda: not self.service._checking)
        self.assertEqual(1, len(self.backend.generated))
        self.assertEqual(1, sum(item.used_for_read for item in self.backend.instances))
        self.assertEqual(2, len(self.backend.instances))

    def test_reconnect_synchronizes_before_restoring_cache_without_another_model_call(self):
        panel = self.service.open_panel("thread-a")["panel_id"]
        initial = self.wait_status(panel, "ready")
        self.eventually(lambda: not self.service._checking and not self.service._jobs)
        self.service.close_panel(panel, "thread-a")
        self.backend.read_gate.clear()
        reopened = self.service.open_panel("thread-a")
        self.assertEqual("syncing", reopened["status"])
        self.assertEqual([], reopened["suggestions"])
        self.assertEqual(initial["revision"], reopened["revision"])
        self.assertEqual(1, reopened["usage"]["calls"])
        self.backend.read_gate.set()
        restored = self.wait_status(reopened["panel_id"], "ready")
        self.eventually(lambda: not self.service._checking)
        self.assertEqual(initial["suggestions"], restored["suggestions"])
        self.assertEqual(1, len(self.backend.generated))

    def test_reconnect_detects_a_turn_started_while_hidden_and_discards_cache(self):
        panel = self.service.open_panel("thread-a")["panel_id"]
        self.wait_status(panel, "ready")
        self.eventually(lambda: not self.service._checking and not self.service._jobs)
        self.service.close_panel(panel, "thread-a")
        self.backend.set_thread(thread_data(turn_id="turn-2", status="inProgress"))
        self.backend.read_gate.clear()
        reopened = self.service.open_panel("thread-a")
        self.assertEqual("syncing", reopened["status"])
        self.backend.read_gate.set()
        current = self.wait_status(reopened["panel_id"], "waiting")
        self.eventually(lambda: not self.service._checking)
        self.assertEqual([], current["suggestions"])
        self.assertGreater(current["revision"], reopened["revision"])
        self.assertEqual(1, len(self.backend.generated))

    def test_close_one_of_two_panels_does_not_discard_result(self):
        self.backend.generate_gate.clear()
        first = self.service.open_panel("thread-a")["panel_id"]
        self.assertTrue(self.backend.generate_entered.wait(1))
        second = self.service.open_panel("thread-a")["panel_id"]
        self.service.close_panel(first, "thread-a")
        self.backend.generate_gate.set()
        self.assertEqual(1, self.wait_status(second, "ready")["usage"]["calls"])
        with self.assertRaises(ValueError):
            self.service.poll(second, "thread-b")

    def test_generation_rechecks_latest_turn_before_publishing(self):
        self.backend.generate_gate.clear()
        panel = self.service.open_panel("thread-a")["panel_id"]
        self.assertTrue(self.backend.generate_entered.wait(1))
        self.backend.set_thread(thread_data(turn_id="turn-2", status="inProgress"))
        self.backend.generate_gate.set()
        self.eventually(lambda: not self.service._jobs)
        snap = self.service.store.snapshot(panel, "thread-a")
        self.assertEqual([], snap["suggestions"])
        self.assertEqual("waiting", snap["status"])
        self.assertEqual(5, snap["usage"]["output_tokens"])
        self.assertGreaterEqual(self.backend.reads, 2)

    def test_expired_result_discarded_but_billed_usage_retained(self):
        self.backend.generate_gate.clear()
        panel = self.service.open_panel("thread-a")["panel_id"]
        self.assertTrue(self.backend.generate_entered.wait(1))
        now = time.time()
        self.service.store.clock = lambda: now + 20
        self.backend.generate_gate.set()
        self.eventually(lambda: not self.service._jobs)
        snap = self.service.store.snapshot(panel, "thread-a")
        self.assertEqual("paused", snap["status"])
        self.assertEqual([], snap["suggestions"])
        self.assertEqual(17, snap["usage"]["input_tokens"])
        self.assertEqual(1, snap["usage"]["calls"])
        self.assertEqual(1, len(self.backend.generated))

    def test_completed_idle_chat_shows_fixed_timeout_error(self):
        self.backend.set_thread(thread_data() | {"status": {"type": "idle"}})
        self.backend.failure = AdapterError("provider-secret-value", code="timeout")
        panel = self.service.open_panel("thread-a")["panel_id"]
        def timeout_snapshot():
            value = self.service.poll(panel, "thread-a")
            return value if value["status"] == "error" else None
        snap = self.eventually(timeout_snapshot)
        self.assertEqual("建议模型调用超过 60 秒，请手动重试。", snap["error"])
        self.assertNotIn("provider-secret", snap["error"])
        self.assertEqual(1, snap["usage"]["calls"])
        self.assertEqual(1, snap["usage"]["failed_calls"])
        self.assertNotIn("input_tokens", snap["usage"])

    def test_known_generation_codes_publish_only_fixed_messages_and_keep_known_usage(self):
        expected = {
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
        private = "synthetic-provider-secret https://private.example/token"
        self.backend.set_thread(thread_data() | {"status": {"type": "idle"}})
        panel = None
        for index, (code, message) in enumerate(expected.items(), 1):
            with self.subTest(code=code):
                self.backend.failure = AdapterError(private, code=code,
                    usage={"input_tokens": 11, "output_tokens": 3, "bad": private})
                if panel is None:
                    panel = self.service.open_panel("thread-a")["panel_id"]
                else:
                    self.service.refresh(panel, "thread-a")
                def failed_snapshot():
                    value = self.service.poll(panel, "thread-a")
                    return value if value["status"] == "error" and value["usage"]["calls"] == index else None
                snap = self.eventually(failed_snapshot)
                self.assertEqual(message, snap["error"])
                self.assertEqual([], snap["suggestions"])
                self.assertEqual(index, snap["usage"]["failed_calls"])
                self.assertEqual(index * 11, snap["usage"]["input_tokens"])
                self.assertEqual(index * 3, snap["usage"]["output_tokens"])
                self.assertNotIn("bad", snap["usage"])
                self.assertNotIn("synthetic-provider-secret", str(snap))
                self.assertNotIn("private.example", str(snap))
                self.service.poll(panel, "thread-a")
                self.eventually(lambda: not self.service._checking and not self.service._jobs)
                self.assertEqual(index, len(self.backend.generated))

    def test_unknown_and_raw_generation_errors_remain_redacted(self):
        raw = RuntimeError("provider-secret-value")
        raw.code = "timeout"  # An arbitrary exception cannot opt into the whitelist.
        errors = [AdapterError("provider-secret-value"),
                  AdapterError("provider-secret-value", code="unknown-provider-code"),
                  AdapterError("provider-secret-value", code="https://provider-secret.example/token"),
                  AdapterError("provider-secret-value", code=None),
                  AdapterError("provider-secret-value", code={"private": "provider-secret-value"}), raw]
        self.backend.set_thread(thread_data() | {"status": {"type": "idle"}})
        panel = None
        for index, error in enumerate(errors, 1):
            with self.subTest(error_type=type(error).__name__, code=getattr(error, "code", None)):
                self.backend.failure = error
                if panel is None:
                    panel = self.service.open_panel("thread-a")["panel_id"]
                else:
                    self.service.refresh(panel, "thread-a")
                def failed_snapshot():
                    value = self.service.poll(panel, "thread-a")
                    return value if value["status"] == "error" and value["usage"]["calls"] >= index else None
                snap = self.eventually(failed_snapshot)
                self.assertEqual("生成建议失败，请检查独立模型配置后手动刷新。", snap["error"])
                self.assertNotIn("provider-secret", snap["error"])

    def test_failed_request_preserves_usage_and_never_auto_retries(self):
        error = RuntimeError("provider leaked-secret-value")
        error.usage = {"input_tokens": 11}
        self.backend.failure = error
        panel = self.service.open_panel("thread-a")["panel_id"]
        snap = self.wait_status(panel, "error")
        self.assertNotIn("leaked-secret", snap["error"])
        self.assertEqual(11, snap["usage"]["input_tokens"])
        self.assertEqual(1, snap["usage"]["failed_calls"])
        self.service.poll(panel, "thread-a")
        self.eventually(lambda: not self.service._checking)
        self.assertEqual(1, len(self.backend.generated))
        self.backend.failure = None
        self.service.refresh(panel, "thread-a")
        snap = self.wait_status(panel, "ready")
        self.assertEqual(2, snap["usage"]["calls"])

    def test_paused_panel_only_generates_after_manual_refresh(self):
        self.service.configure({"auto_generate": False})
        panel = self.service.open_panel("thread-a")["panel_id"]
        self.eventually(lambda: not self.service._checking)
        self.assertEqual([], self.backend.generated)
        self.service.refresh(panel, "thread-a")
        self.wait_status(panel, "ready")
        self.assertEqual(1, len(self.backend.generated))

    def test_configuration_change_discards_old_result_and_retains_old_usage(self):
        self.backend.generate_gate.clear()
        panel = self.service.open_panel("thread-a")["panel_id"]
        self.assertTrue(self.backend.generate_entered.wait(1))
        self.service.set_auto(panel, "thread-a", False)
        self.service.configure({"model": "fake-other-model"})
        self.backend.generate_gate.set()
        self.eventually(lambda: not self.service._jobs and not self.service._checking)
        snap = self.service.store.snapshot(panel, "thread-a")
        self.assertEqual([], snap["suggestions"])
        self.assertEqual(1, snap["usage"]["calls"])
        self.assertEqual(17, snap["usage"]["input_tokens"])
        self.assertEqual(1, len(self.backend.generated))

    def test_two_mcp_services_share_deduplication_and_usage(self):
        self.backend.generate_gate.clear()
        other = self.make_service()
        first = self.service.open_panel("thread-a")["panel_id"]
        second = other.open_panel("thread-a")["panel_id"]
        self.assertTrue(self.backend.generate_entered.wait(1))
        self.eventually(lambda: not self.service._checking and not other._checking)
        self.assertEqual(1, len(self.backend.generated))
        self.backend.generate_gate.set()
        self.wait_status(first, "ready")
        snap = self.wait_status(second, "ready", other)
        self.assertEqual(1, snap["usage"]["calls"])

    def test_model_list_does_not_configure_model_or_generate(self):
        self.service.configure({"model": ""})
        self.assertEqual([{"id": "fake-model"}], self.service.models())
        self.assertEqual("", self.service.get_settings()["model"])
        self.assertEqual([], self.backend.generated)

    def test_unsaved_relay_catalog_never_uses_cli_or_changes_saved_settings(self):
        before = (self.root / "config.json").read_bytes()
        source = {"provider": "relay", "base_url": "https://draft-relay.example/v1", "api_key_env": "DRAFT_KEY"}
        with patch("mogoo_codex_next.service.relay_models", return_value=[{"id": "relay-only"}]) as relay:
            self.assertEqual([{"id": "relay-only"}], self.service.models(source))
        self.assertEqual(source["base_url"], relay.call_args.args[0]["base_url"])
        self.assertEqual([], self.backend.instances)
        self.assertEqual([], self.backend.generated)
        self.assertEqual(before, (self.root / "config.json").read_bytes())
        self.assertEqual("official", self.service.get_settings()["provider"])

    def test_incomplete_catalog_source_never_reads_official_models(self):
        with patch("mogoo_codex_next.service.relay_models") as relay:
            with self.assertRaises(ValueError):
                self.service.models({"provider": "relay"})
            relay.assert_not_called()
        self.assertEqual([], self.backend.instances)

    def test_draft_official_cli_is_isolated_and_closed_after_catalog_read(self):
        before = (self.root / "config.json").read_bytes()
        self.assertEqual([{"id": "fake-model"}], self.service.models({"provider": "official", "codex_bin": "draft-codex"}))
        self.assertEqual("draft-codex", self.backend.instances[0].config["codex_bin"])
        self.assertTrue(self.backend.instances[0].closed)
        self.assertIsNone(self.service._reader)
        self.assertEqual([], self.backend.generated)
        self.assertEqual(before, (self.root / "config.json").read_bytes())

    def test_transient_thread_read_failure_recovers_cache_without_model_retry(self):
        panel = self.service.open_panel("thread-a")["panel_id"]
        self.wait_status(panel, "ready")
        self.backend.read_failure = True
        self.service.poll(panel, "thread-a")
        self.wait_status(panel, "error")
        self.backend.read_failure = False
        self.service.poll(panel, "thread-a")
        snap = self.wait_status(panel, "ready")
        self.assertEqual(1, len(self.backend.generated))
        self.assertEqual(1, snap["usage"]["calls"])


if __name__ == "__main__":
    unittest.main()
