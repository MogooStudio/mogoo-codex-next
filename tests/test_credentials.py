import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from mogoo_codex_next.adapter import CodexAdapter
from mogoo_codex_next.catalog import CatalogError, catalog_config, relay_models
from mogoo_codex_next.config import DEFAULTS, config_fingerprint, is_configured, load_config, public_config, save_config
from mogoo_codex_next.credentials import CredentialError, protect, resolve_key, unprotect

KEY = "synthetic-test-key-for-local-only"
BASE = "https://relay.example/v1"


@unittest.skipUnless(os.name == "nt", "Windows account protection")
class CredentialsTests(unittest.TestCase):
    def test_system_account_roundtrip_and_corruption_redaction(self):
        stored = protect(KEY)
        self.assertNotIn(KEY, stored)
        self.assertEqual(KEY, unprotect(stored))
        with self.assertRaises(CredentialError) as caught:
            unprotect("dpapi:broken-private-value")
        self.assertNotIn("private", str(caught.exception))

    def test_saved_key_is_write_only_preserved_replaced_and_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = save_config(root, {"provider": "relay", "model": "independent", "reasoning_effort": "low", "base_url": BASE, "api_key": KEY})
            self.assertNotIn(KEY, (root / "config.json").read_text())
            self.assertNotIn(KEY, json.dumps(public_config(config)))
            self.assertNotIn("api_key_protected", public_config(config))
            self.assertTrue(public_config(config)["api_key_present"])
            self.assertTrue(is_configured(config))
            self.assertEqual(KEY, resolve_key(save_config(root, {"api_key": "", "model": "next"})))
            previous = config_fingerprint(load_config(root))
            config = save_config(root, {"api_key": KEY + "2"})
            self.assertNotEqual(previous, config_fingerprint(config))
            self.assertEqual(KEY + "2", resolve_key(config))
            self.assertEqual("low", config["reasoning_effort"])
            config = save_config(root, {"clear_api_key": True})
            self.assertFalse(public_config(config)["api_key_present"])
            self.assertFalse(is_configured(config))
            self.assertEqual("", resolve_key(config))

    def test_bad_save_leaves_previous_configuration_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(root, {"base_url": BASE, "api_key": KEY})
            previous = (root / "config.json").read_bytes()
            for changes in ({"api_key": "bad\nkey"}, {"api_key": KEY, "clear_api_key": True},
                            {"api_key": None}, {"base_url": "https://different.example/v1"},
                            {"api_key_protected": protect(KEY)}):
                with self.assertRaises(ValueError):
                    save_config(root, changes)
                self.assertEqual(previous, (root / "config.json").read_bytes())
            config = save_config(root, {"base_url": "https://different.example/v1", "api_key": KEY + "new"})
            self.assertEqual(KEY + "new", resolve_key(config))

    def test_reverted_local_mode_does_not_read_codex_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text(json.dumps(DEFAULTS | {"relay_source": "codex", "model": "independent"}))
            config = load_config(root)
            self.assertNotIn("relay_source", config)
            self.assertEqual("independent", config["model"])
            self.assertEqual("", config["base_url"])

    def test_directory_can_use_unsaved_or_saved_key_only_for_matching_address(self):
        saved = DEFAULTS | {"provider": "relay", "base_url": BASE, "api_key_protected": protect(KEY)}
        source = {"provider": "relay", "base_url": BASE}
        self.assertEqual(KEY, resolve_key(catalog_config(source, saved)))
        self.assertEqual(KEY + "draft", resolve_key(catalog_config(source | {"api_key": KEY + "draft"}, saved)))
        self.assertEqual(KEY, resolve_key(saved))
        with self.assertRaises(ValueError):
            catalog_config(source | {"base_url": "https://different.example/v1"}, saved)

    def test_actual_directory_request_uses_entered_key_without_returning_it(self):
        config = catalog_config({"provider": "relay", "base_url": BASE, "api_key": KEY})
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[{"id":"direct-model"}]}'
        opener = Mock(); opener.open.return_value = response
        with patch("mogoo_codex_next.catalog.urllib.request.build_opener", return_value=opener):
            result = relay_models(config)
        self.assertEqual("Bearer " + KEY, opener.open.call_args.args[0].get_header("Authorization"))
        self.assertEqual([{"id": "direct-model"}], result)
        self.assertNotIn(KEY, json.dumps(result))

    def test_generation_passes_key_only_to_child_environment(self):
        config = DEFAULTS | {"provider": "relay", "model": "independent", "base_url": BASE, "api_key_protected": protect(KEY)}
        adapter = CodexAdapter(config); adapter._binary = "test-codex"
        def run(args, **kwargs):
            self.assertNotIn(KEY, " ".join(args))
            self.assertEqual(KEY, kwargs["env"]["MOGOO_CODEX_NEXT_API_KEY"])
            self.assertEqual("independent", args[args.index("-m") + 1])
            Path(args[args.index("--output-last-message") + 1]).write_text('{"suggestions":[]}')
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("mogoo_codex_next.adapter.subprocess.run", side_effect=run):
            self.assertEqual([], adapter.generate({})["suggestions"])

    def test_unreadable_saved_key_is_redacted_and_never_used(self):
        config = DEFAULTS | {"provider": "relay", "model": "x", "base_url": BASE, "api_key_protected": "dpapi:broken-private"}
        self.assertFalse(public_config(config)["api_key_present"])
        self.assertNotIn("broken-private", json.dumps(public_config(config)))
        with patch("mogoo_codex_next.catalog.urllib.request.build_opener") as network:
            with self.assertRaises(CatalogError):
                relay_models(config)
            network.assert_not_called()
