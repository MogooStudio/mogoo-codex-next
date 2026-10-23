import io
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from mogoo_codex_next.server import McpServer, RESOURCE_URI, RpcError, TOOLS, resource_html
from mogoo_codex_next.catalog import CatalogError, catalog_config
from unittest.mock import patch


class FakeService:
    def __init__(self):
        self.calls = []
        self.stopped = False

    def get_settings(self):
        return {"provider": "official", "model": ""}

    def open_panel(self, thread):
        self.calls.append(("open", thread))
        return {"panel_id": "panel_1", "thread_id": thread, "status": "unconfigured"}

    def poll(self, panel, thread):
        self.calls.append(("poll", panel, thread))
        if thread != "thread_1":
            raise PermissionError("面板不属于该聊天")
        return {"status": "ready", "suggestions": []}

    def configure(self, changes):
        return changes

    def models(self, source=None):
        if source is not None:
            catalog_config(source)
        self.calls.append(("models", source))
        return [{"id": "source-model"}]

    def get_appearance(self):
        return {"skin": "graphite"}

    def set_appearance(self, skin):
        self.calls.append(("appearance", skin))
        return {"skin": skin}

    def shutdown(self):
        self.stopped = True


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.server = McpServer(self.service, ROOT)

    def call(self, name, arguments=None, thread=None):
        params = {"name": name, "arguments": arguments or {}}
        if thread:
            params["_meta"] = {"threadId": thread}
        return self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})

    def test_thread_entrypoint_and_app_only_internal_tools(self):
        panel = TOOLS[0]
        self.assertEqual(panel["_meta"]["openai/ui"]["entrypoints"], [{"type": "thread"}])
        self.assertTrue(all(t["_meta"]["ui"]["visibility"] == ["app"] for t in TOOLS[1:]))

    def test_missing_host_identity_does_not_open_or_generate(self):
        result = self.call("next_panel")
        self.assertEqual(result["structuredContent"]["status"], "unbound")
        self.assertEqual(self.service.calls, [])

    def test_user_arguments_cannot_impersonate_owning_thread(self):
        with self.assertRaises(RpcError):
            self.call("next_panel", {"threadId": "thread_2"}, "thread_1")

    def test_display_preference_is_app_only_and_does_not_open_a_chat(self):
        info = next(t for t in TOOLS if t["name"] == "next_appearance")
        self.assertEqual(info["_meta"]["ui"]["visibility"], ["app"])
        self.assertEqual(self.call("next_appearance")["structuredContent"]["appearance"], {"skin": "graphite"})
        self.call("next_appearance", {"skin": "annotation"})
        self.assertEqual(self.service.calls, [("appearance", "annotation")])
        with self.assertRaises(RpcError):
            self.call("next_appearance", {"skin": "unknown"})

    def test_host_identity_binds_panel_and_poll(self):
        result = self.call("next_panel", thread="thread_1")
        self.assertEqual(result["structuredContent"]["thread_id"], "thread_1")
        self.call("next_poll", {"panel_id": "panel_1"}, "thread_1")
        self.assertEqual(self.service.calls[-1], ("poll", "panel_1", "thread_1"))

    def test_wrong_thread_rejected_by_service(self):
        result = self.call("next_poll", {"panel_id": "panel_1"}, "thread_2")
        self.assertTrue(result["isError"])
        self.assertNotIn("structuredContent", result)

    def test_poll_requires_host_identity_and_valid_panel(self):
        with self.assertRaises(RpcError):
            self.call("next_poll", {"panel_id": "panel_1"})
        with self.assertRaises(RpcError):
            self.call("next_poll", {"panel_id": "../other"}, "thread_1")

    def test_resource_is_self_contained(self):
        html = resource_html(ROOT)
        self.assertNotIn('href="panel.css"', html)
        self.assertNotIn('src="panel.js"', html)
        self.assertIn("<style>", html)
        self.assertIn('send: false', html)
        result = self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": RESOURCE_URI}})
        self.assertEqual(result["contents"][0]["mimeType"], "text/html;profile=mcp-app")

    def test_catalog_forwards_unsaved_source_without_configuring_or_opening_chat(self):
        source = {"provider": "relay", "base_url": "https://relay.example/v1", "api_key_env": "CATALOG_KEY"}
        result = self.call("next_models", {"source": source})["structuredContent"]
        self.assertEqual("relay", result["provider"])
        self.assertEqual([{"id": "source-model"}], result["models"])
        self.assertEqual([("models", source)], self.service.calls)
        info = next(t for t in TOOLS if t["name"] == "next_models")
        self.assertTrue(info["annotations"]["readOnlyHint"])

    def test_invalid_catalog_sources_are_rejected_without_exposing_values(self):
        for source in (None, "not-object", {}, {"provider": "other"}, {"provider": "official", "api_key": ["invalid"]}):
            with self.subTest(source=source):
                result = self.call("next_models", {"source": source})
                self.assertTrue(result["isError"])
                self.assertNotIn("synthetic-private-key", json.dumps(result))
        self.assertEqual([], self.service.calls)

    def test_catalog_without_explicit_source_uses_saved_provider(self):
        result = self.call("next_models")["structuredContent"]
        self.assertEqual("official", result["provider"])
        self.assertEqual([("models", None)], self.service.calls)

    def test_fixed_catalog_errors_reach_the_ui_with_manual_input_guidance(self):
        with patch.object(self.service, "models", side_effect=CatalogError("catalog_unsupported")):
            result = self.call("next_models", {"source": {"provider": "relay"}})
        self.assertTrue(result["isError"])
        self.assertIn("中转服务未提供模型目录", result["content"][0]["text"])
        self.assertIn("手动输入", result["content"][0]["text"])

    def test_stdio_recovers_from_bad_json_and_shuts_down(self):
        source = io.StringIO('bad json\n' + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + '\n')
        target = io.StringIO()
        self.server.run(source, target)
        responses = [json.loads(x) for x in target.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})
        self.assertTrue(self.service.stopped)

    def test_protocol_negotiation_does_not_claim_unimplemented_discovery(self):
        result = self.server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
        self.assertEqual(result["protocolVersion"], "2025-11-25")
        with self.assertRaises(RpcError) as error:
            self.server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "server/discover"})
        self.assertEqual(error.exception.code, -32601)
        result = self.server.dispatch({"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {"protocolVersion": "2026-07-28"}})
        self.assertEqual(result["protocolVersion"], "2025-11-25")


if __name__ == "__main__":
    unittest.main()
