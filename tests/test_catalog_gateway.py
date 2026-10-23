import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from mogoo_codex_next.catalog import catalog_config, relay_models
from mogoo_codex_next.config import save_config
from mogoo_codex_next.service import NextService


class CatalogGatewayTests(unittest.TestCase):
    def response(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"data":[{"id":"gpt-6.1-sol"}]}'
        return response

    def test_documented_codex_gateways_list_same_origin_generic_models_with_own_agent(self):
        for host in ("api.aicodewith.ai", "api.aicodewith.com", "api.with7.cn"):
            with self.subTest(host=host):
                source = {"provider": "relay", "base_url": "https://" + host + "/chatgpt/v1/", "api_key_env": "GATEWAY_TEST_KEY"}
                config = catalog_config(source)
                before = dict(config)
                opener = Mock()
                def open_request(request, **kwargs):
                    if not request.get_header("User-agent", "").startswith("Mogoo-Codex-Next/"):
                        raise HTTPError(request.full_url, 403, "client rejected", {}, io.BytesIO())
                    if request.full_url != "https://" + host + "/v1/models":
                        raise HTTPError(request.full_url, 400, "not a catalog route", {}, io.BytesIO())
                    self.assertEqual("Bearer synthetic-gateway-key", request.get_header("Authorization"))
                    self.assertEqual("GET", request.get_method())
                    return self.response()
                opener.open.side_effect = open_request
                with patch.dict(os.environ, {"GATEWAY_TEST_KEY": "synthetic-gateway-key"}), patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
                    self.assertEqual([{"id": "gpt-6.1-sol"}], relay_models(config))
                self.assertEqual(before, config)
                opener.open.assert_called_once()

    def test_other_origins_or_custom_paths_are_not_rewritten(self):
        for base in ("https://relay.example/chatgpt/v1", "https://api.aicodewith.ai/custom/v1", "https://api.aicodewith.ai/v1", "https://api.aicodewith.ai.attacker.test/chatgpt/v1"):
            with self.subTest(base=base):
                config = catalog_config({"provider": "relay", "base_url": base, "api_key_env": "GATEWAY_TEST_KEY"})
                opener = Mock(); opener.open.return_value = self.response()
                with patch.dict(os.environ, {"GATEWAY_TEST_KEY": "synthetic-gateway-key"}), patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
                    self.assertEqual([{"id": "gpt-6.1-sol"}], relay_models(config))
                request = opener.open.call_args.args[0]
                self.assertEqual(base + "/models", request.full_url)

    @unittest.skipUnless(os.name == "nt", "Windows protected credentials")
    def test_service_catalog_leaves_saved_generation_settings_and_jobs_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(root, {"provider": "relay", "base_url": "https://api.aicodewith.ai/chatgpt/v1", "api_key": "synthetic-service-key", "model": "gpt-6.1-sol", "reasoning_effort": "low"})
            before = (root / "config.json").read_bytes()
            adapter = Mock(side_effect=AssertionError("catalog must not construct a generation adapter"))
            service = NextService(root, adapter_factory=adapter)
            opener = Mock(); opener.open.return_value = self.response()
            try:
                with patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
                    models = service.models({"provider": "relay", "base_url": "https://api.aicodewith.ai/chatgpt/v1"})
                self.assertEqual([{"id": "gpt-6.1-sol"}], models)
                self.assertEqual("https://api.aicodewith.ai/v1/models", opener.open.call_args.args[0].full_url)
                self.assertEqual(before, (root / "config.json").read_bytes())
                self.assertEqual("gpt-6.1-sol", service.get_settings()["model"])
                self.assertEqual("low", service.get_settings()["reasoning_effort"])
                with service.store._connection() as db:
                    self.assertEqual(0, db.execute("SELECT count(*) FROM jobs").fetchone()[0])
                adapter.assert_not_called()
            finally:
                service.shutdown()
