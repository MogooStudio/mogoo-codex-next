import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mogoo_codex_next.adapter import AdapterError, CodexAdapter, _parse_events, _windows_binary, find_codex, worker_env
from mogoo_codex_next.config import DEFAULTS


class AdapterTests(unittest.TestCase):
    def setUp(self):
        # Ordinary offline tests must not consult real machine proxy settings.
        self.proxy_discovery = patch("urllib.request.getproxies", return_value={})
        self.proxy_discovery.start()
        self.addCleanup(self.proxy_discovery.stop)

    def test_windows_worker_uses_discovered_proxies_without_changing_process_environment(self):
        found = {"http": "http://http.example:8080", "https": "http://https.example:8080",
                 "all": "socks5://all.example:1080", "no": "localhost,*.local",
                 "ftp": "http://ignored.example:8080"}
        with patch.dict(os.environ, {"KEEP": "value"}, clear=True), \
                patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                patch("urllib.request.getproxies", return_value=found):
            original = dict(os.environ)
            env = worker_env()
            self.assertEqual("http://http.example:8080", env.get("HTTP_PROXY"))
            self.assertEqual("http://https.example:8080", env.get("HTTPS_PROXY"))
            self.assertEqual("socks5://all.example:1080", env.get("ALL_PROXY"))
            self.assertEqual("localhost,*.local", env.get("NO_PROXY"))
            self.assertNotIn("FTP_PROXY", env)
            self.assertEqual(original, dict(os.environ))

    def test_windows_no_proxy_only_still_discovers_system_transport_proxy(self):
        with patch.dict(os.environ, {"NO_PROXY": "localhost,*.internal"}, clear=True), \
                patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                patch("urllib.request.getproxies", return_value={"no": "localhost,*.internal"}), \
                patch("urllib.request.getproxies_registry", create=True,
                      return_value={"http": "http://system.example:8080", "no": "registry-bypass"}):
            original = dict(os.environ)
            env = worker_env()
            self.assertEqual("http://system.example:8080", env.get("HTTP_PROXY"))
            self.assertEqual("localhost,*.internal", env.get("NO_PROXY"))
            self.assertEqual(original, dict(os.environ))

    def test_explicit_transport_proxy_including_empty_prevents_discovery(self):
        for name in ("http_proxy", "HTTPS_PROXY", "All_Proxy"):
            for value in ("http://explicit.example:8080", ""):
                with self.subTest(name=name, empty=not value), \
                        patch.dict(os.environ, {name: value, "NO_PROXY": "explicit-bypass"}, clear=True), \
                        patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                        patch("urllib.request.getproxies", side_effect=AssertionError("must not discover")) as discover:
                    original = dict(os.environ)
                    self.assertEqual(original | {"MOGOO_CODEX_NEXT_WORKER": "1"}, worker_env())
                    self.assertEqual(original, dict(os.environ))
                    discover.assert_not_called()

    def test_explicit_empty_bypass_is_preserved_with_system_transport_proxy(self):
        with patch.dict(os.environ, {"NO_PROXY": ""}, clear=True), \
                patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                patch("urllib.request.getproxies", return_value={"https": "http://system.example:8080", "no": "system-bypass"}):
            env = worker_env()
            self.assertEqual("", env.get("NO_PROXY"))
            self.assertEqual("http://system.example:8080", env.get("HTTPS_PROXY"))

    def test_proxy_discovery_failure_preserves_original_worker_environment(self):
        with patch.dict(os.environ, {"KEEP": "value"}, clear=True), \
                patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                patch("urllib.request.getproxies", side_effect=OSError("not available")):
            self.assertEqual({"KEEP": "value", "MOGOO_CODEX_NEXT_WORKER": "1"}, worker_env())
        with patch.dict(os.environ, {"NO_PROXY": "explicit-bypass"}, clear=True), \
                patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                patch("urllib.request.getproxies", return_value={"no": "explicit-bypass"}), \
                patch("urllib.request.getproxies_registry", create=True, side_effect=OSError("not available")):
            self.assertEqual({"NO_PROXY": "explicit-bypass", "MOGOO_CODEX_NEXT_WORKER": "1"}, worker_env())

    def test_invalid_discovered_proxy_values_are_ignored(self):
        invalid = {"http": None, "https": "http://bad.example\n", "all": 42, "no": " "}
        with patch.dict(os.environ, {"KEEP": "value"}, clear=True), \
                patch("mogoo_codex_next.adapter.sys.platform", "win32"), \
                patch("urllib.request.getproxies", return_value=invalid):
            self.assertEqual({"KEEP": "value", "MOGOO_CODEX_NEXT_WORKER": "1"}, worker_env())

    def test_non_windows_worker_never_discovers_system_proxy(self):
        for platform_name in ("linux", "darwin"):
            with self.subTest(platform=platform_name), \
                    patch.dict(os.environ, {"KEEP": "value"}, clear=True), \
                    patch("mogoo_codex_next.adapter.sys.platform", platform_name), \
                    patch("urllib.request.getproxies", side_effect=AssertionError("must not discover")) as discover:
                self.assertEqual({"KEEP": "value", "MOGOO_CODEX_NEXT_WORKER": "1"}, worker_env())
                discover.assert_not_called()

    def test_environment_detaches_original_thread(self):
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "original", "CODEX_SESSION_ID": "original",
                "CODEX_APP_TOOLS_PIPE_PATH": "original", "TEST_KEY": "keep-private"}):
            env = worker_env()
            self.assertNotIn("CODEX_THREAD_ID", env)
            self.assertNotIn("CODEX_APP_TOOLS_PIPE_PATH", env)
            self.assertEqual(env["TEST_KEY"], "keep-private")

    def test_read_only_rpc_never_resumes_thread(self):
        adapter = CodexAdapter(DEFAULTS)
        ident = "12345678-1234-4234-9234-123456789abc"
        calls = []
        def call(method, params):
            calls.append((method, params))
            return {"thread": {"id": ident}}
        with patch.object(adapter, "_call", side_effect=call):
            self.assertEqual(adapter.read_thread(ident)["id"], ident)
        self.assertEqual(calls, [("thread/read", {"threadId": ident, "includeTurns": True})])
        with self.assertRaises(AdapterError):
            adapter.read_thread("not-a-thread")

    def test_model_list_keeps_catalog_capabilities_and_paginates(self):
        adapter = CodexAdapter(DEFAULTS)
        pages = [{"data": [{"model": "one", "supportedReasoningEfforts": []}], "nextCursor": "next"},
                 {"data": [{"model": "two"}], "nextCursor": None}]
        with patch.object(adapter, "_call", side_effect=pages) as rpc:
            models = adapter.list_models()
        self.assertEqual([m["model"] for m in models], ["one", "two"])
        self.assertEqual(rpc.call_args_list[1].args[1]["cursor"], "next")

    def test_generate_is_ephemeral_isolated_and_reports_usage(self):
        adapter = CodexAdapter(DEFAULTS | {"model": "chosen-test-model", "service_tier": "priority",
                "base_url": "https://previous-relay.example/v1", "api_key_env": "PREVIOUS_RELAY_KEY"})
        adapter._binary = "test-codex"
        captured = {}
        def run(args, **kwargs):
            captured.update(args=args, kwargs=kwargs)
            output = Path(args[args.index("--output-last-message") + 1])
            output.write_text(json.dumps({"suggestions": [{"title": "检查", "prompt": "请检查结果", "reason": "验证完成情况"}]}), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=json.dumps({"type": "turn.completed", "usage": {
                "input_tokens": 12, "output_tokens": 4, "cached_input_tokens": 2}}), stderr="private-stderr")
        with patch("mogoo_codex_next.adapter.subprocess.run", side_effect=run):
            result = adapter.generate({"messages": [{"role": "user", "content": "测试"}]})
        args, kwargs = captured["args"], captured["kwargs"]
        self.assertEqual(args[:2], ["test-codex", "exec"])
        for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--output-schema"):
            self.assertIn(flag, args)
        self.assertNotIn("resume", args)
        self.assertNotIn("turn/start", args)
        self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
        self.assertIn('model_provider="openai"', args)
        self.assertFalse(any("model_providers.mogoo_next_relay" in arg for arg in args))
        self.assertEqual(kwargs["timeout"], 60)
        self.assertNotIn("shell", kwargs)
        self.assertEqual(result["usage"]["input_tokens"], 12)
        self.assertEqual(len(result["suggestions"]), 1)

    def test_relay_only_sends_environment_variable_name_in_arguments(self):
        adapter = CodexAdapter(DEFAULTS | {"provider": "relay", "model": "relay-test-model",
                "base_url": "https://relay.example/v1", "api_key_env": "TEST_RELAY_SECRET"})
        adapter._binary = "test-codex"
        def run(args, **kwargs):
            self.assertNotIn("private-secret", " ".join(args))
            self.assertIn('env_key = "MOGOO_CODEX_NEXT_API_KEY"', " ".join(args))
            self.assertEqual(kwargs["env"]["MOGOO_CODEX_NEXT_API_KEY"], "private-secret")
            Path(args[args.index("--output-last-message") + 1]).write_text('{"suggestions": []}')
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.dict(os.environ, {"TEST_RELAY_SECRET": "private-secret"}), patch(
                "mogoo_codex_next.adapter.subprocess.run", side_effect=run):
            self.assertEqual(adapter.generate({})["suggestions"], [])

    def test_unconfigured_or_missing_key_never_launches(self):
        with patch("mogoo_codex_next.adapter.subprocess.run") as run:
            with self.assertRaises(AdapterError):
                CodexAdapter(DEFAULTS).generate({})
            run.assert_not_called()

    def test_failed_call_redacts_raw_diagnostics_and_preserves_usage(self):
        adapter = CodexAdapter(DEFAULTS | {"model": "test"})
        adapter._binary = "test-codex"
        with patch("mogoo_codex_next.adapter.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout='{"type":"turn.completed","usage":{"input_tokens":9}}',
                stderr="private-credential-and-conversation")):
            with self.assertRaises(AdapterError) as caught:
                adapter.generate({})
        self.assertNotIn("private-credential", str(caught.exception))
        self.assertEqual(caught.exception.usage, {"input_tokens": 9})
        self.assertEqual("process_failed", caught.exception.code)

    def test_generate_classifies_real_failure_boundaries_without_exposing_private_data(self):
        private = "synthetic-provider-secret https://private.example/token"
        usage = {"input_tokens": 19, "output_tokens": 7}
        cases = [
            ("process_nonzero", "process_failed", usage),
            ("missing_output", "output_missing", usage),
            ("tool_item", "tool_attempt", usage),
            ("invalid_json", "output_json", usage),
            ("schema_structure", "output_schema", usage),
            ("schema_count", "output_schema", usage),
            ("schema_item", "output_schema", usage),
            ("schema_text", "output_schema", usage),
            ("schema_length", "output_schema", usage),
            ("process_start", "process_start", {}),
            ("output_read", "output_read", usage),
            ("temp_create", "local_io", {}),
            ("schema_write", "local_io", {}),
            ("policy_write", "local_io", {}),
            ("timeout", "timeout", usage),
        ]
        for scenario, expected_code, expected_usage in cases:
            with self.subTest(scenario=scenario):
                adapter = CodexAdapter(DEFAULTS | {"model": "offline-test"})
                adapter._binary = "never-spawn-codex"
                output = None
                valid_item = {"title": "检查结果", "prompt": "请检查结果", "reason": "验证本轮修改"}
                real_read, real_write = Path.read_text, Path.write_text

                def run(args, **kwargs):
                    nonlocal output
                    if scenario == "process_start":
                        raise OSError(private)
                    events = [{"type": "turn.completed", "usage": usage}]
                    if scenario == "timeout":
                        raise subprocess.TimeoutExpired([private], 60,
                            output=json.dumps(events[0]).encode(), stderr=private)
                    output = Path(args[args.index("--output-last-message") + 1])
                    value = {"suggestions": [dict(valid_item)]}
                    if scenario == "schema_structure":
                        value = {"private": private}
                    elif scenario == "schema_count":
                        value = {"suggestions": [dict(valid_item) for _ in range(4)]}
                    elif scenario == "schema_item":
                        value = {"suggestions": [{"title": "检查", "prompt": private}]}
                    elif scenario == "schema_text":
                        value["suggestions"][0]["title"] = " "
                    elif scenario == "schema_length":
                        value["suggestions"][0]["prompt"] = "x" * 2001
                    if scenario != "missing_output":
                        real_write(output, "{" + private if scenario == "invalid_json" else json.dumps(value), encoding="utf-8")
                    if scenario == "tool_item":
                        events.insert(0, {"type": "item.started", "item": {
                            "type": "command_execution", "command": private}})
                    return SimpleNamespace(returncode=1 if scenario == "process_nonzero" else 0,
                        stdout="\n".join(json.dumps(event) for event in events), stderr=private)

                def read(path, *args, **kwargs):
                    if scenario == "output_read" and path == output:
                        raise PermissionError(private)
                    return real_read(path, *args, **kwargs)

                def write(path, *args, **kwargs):
                    if ((scenario == "schema_write" and path.name == "schema.json")
                            or (scenario == "policy_write" and path.name == "policy.md")):
                        raise PermissionError(private)
                    return real_write(path, *args, **kwargs)

                from contextlib import ExitStack
                with ExitStack() as stack:
                    mocked_run = stack.enter_context(patch("mogoo_codex_next.adapter.subprocess.run", side_effect=run))
                    stack.enter_context(patch.object(Path, "read_text", read))
                    stack.enter_context(patch.object(Path, "write_text", write))
                    if scenario == "temp_create":
                        stack.enter_context(patch("mogoo_codex_next.adapter.tempfile.TemporaryDirectory",
                            side_effect=PermissionError(private)))
                    with self.assertRaises(AdapterError) as caught:
                        adapter.generate({"messages": []})
                self.assertEqual(expected_code, caught.exception.code)
                self.assertEqual(expected_usage, caught.exception.usage)
                self.assertNotIn("synthetic-provider-secret", str(caught.exception))
                self.assertNotIn("private.example", str(caught.exception))
                if scenario in ("temp_create", "schema_write", "policy_write"):
                    mocked_run.assert_not_called()
                else:
                    mocked_run.assert_called_once()

    def test_tool_events_are_rejected_and_usage_not_invented(self):
        usage, forbidden = _parse_events('{"type":"item.completed","item":{"type":"mcp_tool_call"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":true,"output_tokens":-1}}')
        self.assertTrue(forbidden)
        self.assertEqual(usage, {})

    def test_timeout_fixed_error(self):
        adapter = CodexAdapter(DEFAULTS | {"model": "test"})
        adapter._binary = "test-codex"
        with patch("mogoo_codex_next.adapter.subprocess.run", side_effect=subprocess.TimeoutExpired(
                ["private-command"], 60, stderr="private-value")):
            with self.assertRaisesRegex(AdapterError, "60 秒") as caught:
                adapter.generate({})
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual("timeout", caught.exception.code)

    def test_windows_npm_shim_resolves_native_binary_without_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture")
            with patch("mogoo_codex_next.adapter.platform.machine", return_value="AMD64"):
                self.assertEqual(Path(_windows_binary(str(root / "codex.cmd"))).resolve(), binary.resolve())

    def test_discovery_verifies_candidate_and_does_not_cache_broken_entry(self):
        with patch("mogoo_codex_next.adapter.shutil.which", return_value="/test/codex.exe"), patch(
                "mogoo_codex_next.adapter._windows_binary", return_value="/test/codex.exe"), patch(
                "mogoo_codex_next.adapter.subprocess.run", return_value=SimpleNamespace(
                    returncode=0, stdout="codex-cli 0.test")) as run:
            self.assertEqual(find_codex("/explicit/codex.exe"), "/test/codex.exe")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_nonfatal_cli_diagnostic_is_not_a_tool_and_preserves_usage(self):
        events = [
            {"type": "item.completed", "item": {"id": "warning", "type": "error", "message": "Synthetic non-fatal warning"}},
            {"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": '{"suggestions": []}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}},
        ]
        usage, forbidden = _parse_events("\n".join(json.dumps(e) for e in events))
        self.assertFalse(forbidden)
        self.assertEqual(usage, {"input_tokens": 20, "output_tokens": 5})

    def test_generate_accepts_valid_json_after_nonfatal_cli_diagnostic(self):
        adapter = CodexAdapter(DEFAULTS | {"model": "synthetic-model"})
        adapter._binary = "mock-codex"
        def run(args, **kwargs):
            Path(args[args.index("--output-last-message") + 1]).write_text(
                '{"suggestions": [{"title": "检查结果", "prompt": "请检查当前结果", "reason": "核对完成情况"}]}', encoding="utf-8")
            events = [
                {"type": "item.completed", "item": {"type": "error", "message": "Synthetic diagnostic"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "Synthetic final JSON"}},
                {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}},
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(e) for e in events), stderr="")
        with patch("mogoo_codex_next.adapter.subprocess.run", side_effect=run):
            result = adapter.generate({"messages": [{"role": "user", "content": "Synthetic context"}]})
        self.assertEqual(len(result["suggestions"]), 1)
        self.assertEqual(result["usage"], {"input_tokens": 20, "output_tokens": 5})

    def test_tool_action_items_remain_rejected_after_nonfatal_fix(self):
        for kind in ("command_execution", "file_change", "mcp_tool_call", "collab_tool_call", "web_search", "todo_list"):
            with self.subTest(kind=kind):
                events = [
                    {"type": "item.completed", "item": {"type": "error", "message": "Synthetic warning"}},
                    {"type": "item.started", "item": {"type": kind}},
                    {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}},
                ]
                usage, forbidden = _parse_events("\n".join(json.dumps(e) for e in events))
                self.assertTrue(forbidden)
                self.assertEqual(usage["input_tokens"], 20)

    def test_generate_rejects_actual_tool_item_even_with_valid_output(self):
        adapter = CodexAdapter(DEFAULTS | {"model": "synthetic-model"})
        adapter._binary = "mock-codex"
        def run(args, **kwargs):
            Path(args[args.index("--output-last-message") + 1]).write_text('{"suggestions": []}', encoding="utf-8")
            events = [
                {"type": "item.completed", "item": {"type": "error", "message": "Synthetic warning"}},
                {"type": "item.started", "item": {"type": "command_execution", "command": "synthetic-tool"}},
                {"type": "turn.completed", "usage": {"input_tokens": 20, "output_tokens": 5}},
            ]
            return SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(e) for e in events), stderr="")
        with patch("mogoo_codex_next.adapter.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(AdapterError, "尝试调用工具") as caught:
                adapter.generate({"messages": []})
        self.assertEqual(caught.exception.usage, {"input_tokens": 20, "output_tokens": 5})

    def test_unknown_cli_item_type_remains_rejected(self):
        _, forbidden = _parse_events(json.dumps({"type": "item.completed", "item": {"type": "synthetic_unknown_item"}}))
        self.assertTrue(forbidden)
