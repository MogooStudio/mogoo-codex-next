import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mogoo_codex_next.config import (DEFAULTS, config_fingerprint, data_dir, is_configured,
                                    load_config, public_config, save_config)


class ConfigTests(unittest.TestCase):
    def test_default_does_not_write_or_choose_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            value = load_config(root)
            self.assertEqual(value["model"], "")
            self.assertFalse(is_configured(value))
            self.assertFalse((root / "config.json").exists())

    def test_save_merge_atomic_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = save_config(root, {"model": "test-model", "service_tier": "fast"})
            second = save_config(root, {"auto_generate": False})
            self.assertTrue(is_configured(second))
            self.assertEqual(second["model"], "test-model")
            self.assertEqual(second["service_tier"], "priority")
            self.assertNotEqual(config_fingerprint(first), config_fingerprint(second))
            self.assertEqual(load_config(root), second)
            self.assertEqual(list(root.glob(".config-*")), [])

    def test_no_secret_values_in_config_or_public_response(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"TEST_RELAY_KEY": "private-test-value"}):
            root = Path(folder)
            value = save_config(root, {"provider": "relay", "model": "r", "api_key_env": "TEST_RELAY_KEY",
                                       "base_url": "https://relay.example/v1"})
            self.assertTrue(public_config(value)["api_key_present"])
            self.assertNotIn("private-test-value", json.dumps(public_config(value)))
            self.assertNotIn("private-test-value", (root / "config.json").read_text())
            for bad in ({"api_key": ["invalid"]}, {"api_key_env": "not-an-env-key"},
                        {"base_url": "https://user:secret@relay.example/v1"},
                        {"base_url": "https://relay.example/v1?key=secret"},
                        {"auto_generate": "true"}, {"reasoning_effort": "invented"}):
                with self.assertRaises(ValueError):
                    save_config(root, bad)

    def test_relay_incomplete_is_unconfigured(self):
        self.assertFalse(is_configured(DEFAULTS | {"provider": "relay", "model": "r"}))

    def test_provider_switch_public_view_matches_single_active_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            save_config(root, {"model": "official-model", "service_tier": "priority"})
            relay = save_config(root, {"provider": "relay", "model": "relay-model", "service_tier": None,
                                       "base_url": "https://relay.example/v1", "api_key_env": "TEST_RELAY_KEY"})
            self.assertEqual((public_config(relay)["provider"], public_config(relay)["model"],
                              public_config(relay)["service_tier"]), ("relay", "relay-model", None))
            official = save_config(root, {"provider": "official", "model": "official-next-model",
                                          "service_tier": "fast"})
            view = public_config(official)
            self.assertEqual((view["provider"], view["model"], view["service_tier"]),
                             ("official", "official-next-model", "priority"))
            # 单套活动配置，无隐式恢复旧模型；中转字段可保留供再次切换编辑。
            self.assertEqual(official["base_url"], "https://relay.example/v1")
            self.assertEqual(load_config(root), official)

    def test_corrupt_config_has_fixed_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config.json").write_text("private-raw-error", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "无法读取插件配置") as caught:
                load_config(root)
            self.assertNotIn("private-raw-error", str(caught.exception))

    def test_root_override(self):
        with patch.dict(os.environ, {"MOGOO_CODEX_NEXT_HOME": "test-custom-root"}):
            self.assertEqual(data_dir(), Path("test-custom-root"))
