import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError, URLError
import urllib.request

from mogoo_codex_next.catalog import CatalogError, MAX_CATALOG_BYTES, catalog_config, relay_models


SOURCE = {"provider": "relay", "base_url": "https://relay.example/v1/", "api_key_env": "CATALOG_TEST_KEY"}


class CatalogTests(unittest.TestCase):
    def config(self, changes=None):
        return catalog_config(SOURCE | (changes or {}))

    def response(self, payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return response

    def test_incomplete_or_invalid_sources_fail_before_network_access(self):
        cases = [None, {}, {"provider": "other"}, {"provider": "relay"},
                 SOURCE | {"api_key_env": ""}, SOURCE | {"api_key_env": "not a variable"},
                 SOURCE | {"api_key": ["invalid"]},
                 SOURCE | {"base_url": "https://name:password@example.test/v1"},
                 SOURCE | {"base_url": "https://example.test/v1?api_key=private"}]
        with patch("mogoo_codex_next.catalog.urllib.request.build_opener") as network:
            for source in cases:
                with self.subTest(source=source), self.assertRaises(ValueError):
                    catalog_config(source)
            network.assert_not_called()

    def test_relay_models_reads_only_selected_endpoint_and_environment_key(self):
        response = self.response({"data": [{"id": "relay-a", "owner": "private-field"},
                                         {"id": "relay-a"}, {"id": "relay-b"}, {"id": "bad\nmodel"}, "invalid"]})
        opener = Mock(); opener.open.return_value = response
        with patch.dict(os.environ, {"CATALOG_TEST_KEY": "synthetic-private-key"}, clear=True), \
                patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
            result = relay_models(self.config())
        self.assertEqual([{"id": "relay-a"}, {"id": "relay-b"}], result)
        request = opener.open.call_args.args[0]
        self.assertEqual("https://relay.example/v1/models", request.full_url)
        self.assertEqual("GET", request.get_method())
        self.assertEqual("Bearer synthetic-private-key", request.get_header("Authorization"))
        self.assertEqual(10, opener.open.call_args.kwargs["timeout"])
        self.assertNotIn("private", json.dumps(result))
        response.read.assert_called_once_with(MAX_CATALOG_BYTES + 1)

    def test_missing_or_invalid_key_never_contacts_a_service(self):
        for key in ("", "bad\nkey", "x" * 8193):
            with self.subTest(length=len(key)), patch.dict(os.environ, {"CATALOG_TEST_KEY": key}, clear=True), \
                    patch("mogoo_codex_next.catalog.urllib.request.build_opener") as network:
                with self.assertRaises(CatalogError) as caught:
                    relay_models(self.config())
                self.assertIn(caught.exception.code, ("catalog_key_missing", "catalog_key_invalid"))
                network.assert_not_called()

    def test_http_failures_are_redacted_without_official_fallback(self):
        for status, expected in ((401, "catalog_auth"), (403, "catalog_auth"), (404, "catalog_unsupported"),
                                 (405, "catalog_unsupported"), (302, "catalog_redirect"), (500, "catalog_http")):
            body = io.BytesIO(b"synthetic-private-body")
            error = HTTPError("https://private.example/key", status, "synthetic-private-message", {}, body)
            opener = Mock(); opener.open.side_effect = error
            with self.subTest(status=status), patch.dict(os.environ, {"CATALOG_TEST_KEY": "synthetic-private-key"}), \
                    patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(CatalogError) as caught:
                    relay_models(self.config())
                self.assertEqual(expected, caught.exception.code)
                self.assertNotIn("private", str(caught.exception))
                self.assertIn("手动输入", str(caught.exception))
                self.assertTrue(body.closed)
                opener.open.assert_called_once()

    def test_network_and_unrecognized_responses_do_not_leak_raw_data(self):
        for error in (URLError("synthetic-private-url"), TimeoutError("private-timeout")):
            opener = Mock(); opener.open.side_effect = error
            with patch.dict(os.environ, {"CATALOG_TEST_KEY": "synthetic-private-key"}), \
                    patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(CatalogError) as caught:
                    relay_models(self.config())
                self.assertEqual("catalog_network", caught.exception.code)
                self.assertNotIn("private", str(caught.exception))
        for payload, code in ((b"private-invalid-json", "catalog_format"), ({"models": []}, "catalog_format"),
                              ({"data": [{"id": ""}]}, "catalog_format"), (b"x" * (MAX_CATALOG_BYTES + 1), "catalog_size")):
            opener = Mock(); opener.open.return_value = self.response(payload)
            with self.subTest(code=code), patch.dict(os.environ, {"CATALOG_TEST_KEY": "synthetic-private-key"}), \
                    patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(CatalogError) as caught:
                    relay_models(self.config())
                self.assertEqual(code, caught.exception.code)
                self.assertNotIn("private", str(caught.exception))

    def test_empty_catalog_is_valid(self):
        opener = Mock(); opener.open.return_value = self.response({"data": []})
        with patch.dict(os.environ, {"CATALOG_TEST_KEY": "synthetic-private-key"}), \
                patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
            self.assertEqual([], relay_models(self.config()))

    def test_real_http_request_and_redirect_do_not_forward_credentials(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect/models":
                    self.send_response(302)
                    self.send_header("Location", "/credential-target")
                    self.end_headers()
                    return
                body = json.dumps({"data": [{"id": "local-relay-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        build_opener = urllib.request.build_opener
        try:
            with patch.dict(os.environ, {"CATALOG_TEST_KEY": "synthetic-local-key"}), \
                    patch("mogoo_codex_next.catalog.urllib.request.build_opener", side_effect=lambda *handlers: build_opener(urllib.request.ProxyHandler({}), *handlers)):
                base = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual([{"id": "local-relay-model"}], relay_models(self.config({"base_url": base + "/v1"})))
                with self.assertRaises(CatalogError) as caught:
                    relay_models(self.config({"base_url": base + "/redirect"}))
                self.assertEqual("catalog_redirect", caught.exception.code)
                self.assertEqual([("/v1/models", "Bearer synthetic-local-key"), ("/redirect/models", "Bearer synthetic-local-key")], requests)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
