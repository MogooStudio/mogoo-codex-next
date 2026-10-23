import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mogoo_codex_next.appearance import load_appearance, save_appearance
from mogoo_codex_next.config import config_fingerprint, load_config, save_config
from mogoo_codex_next.service import NextService


class AppearanceTests(unittest.TestCase):
    def test_default_read_does_not_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.assertEqual(load_appearance(root), {"skin": "graphite"})
            self.assertEqual(list(root.iterdir()), [])

    def test_reload_and_failed_write_preserve_previous_choice_and_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = save_config(root, {"model": "chosen"})
            original = (root / "config.json").read_bytes()
            for skin in ("graphite", "annotation", "focus"):
                save_appearance(root, skin)
                self.assertEqual(load_appearance(root), {"skin": skin})
            with patch("mogoo_codex_next.appearance.os.replace", side_effect=OSError("private-path")):
                with self.assertRaisesRegex(ValueError, "外观没有保存"):
                    save_appearance(root, "annotation")
            self.assertEqual(load_appearance(root), {"skin": "focus"})
            self.assertEqual((root / "config.json").read_bytes(), original)
            self.assertEqual(config_fingerprint(config), config_fingerprint(load_config(root)))
            self.assertEqual(list(root.glob(".appearance-*")), [])

    def test_invalid_or_corrupt_preferences_are_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for bad in ("other", None, [], {"skin": "graphite"}):
                with self.assertRaises(ValueError):
                    save_appearance(root, bad)
            for bad in ("private-sensitive-value", json.dumps({"skin": "unknown"})):
                (root / "appearance.json").write_text(bad, encoding="utf-8")
                loaded = load_appearance(root)
                self.assertEqual(loaded["skin"], "graphite")
                self.assertNotIn("private", str(loaded))

    def test_service_display_preference_never_schedules_or_constructs_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = save_config(root, {"model": "chosen"})
            service = NextService(root, adapter_factory=lambda _: self.fail("adapter constructed"))
            try:
                panel = service.store.open_panel("thread-a", True)
                service.store.observe("thread-a", {"thread_id": "thread-a", "turn_id": "turn-a", "fingerprint": "content-a"}, config_fingerprint(config))
                job = service.store.queue_job("thread-a", config_fingerprint(config))
                self.assertTrue(service.store.begin_job(job["id"]))
                self.assertTrue(service.store.mark_invoked(job["id"]))
                service.store.finish_job(job["id"], [{"prompt": "keep-this"}], {"input_tokens": 2, "output_tokens": 1})
                before = service.store.snapshot(panel, "thread-a")
                with patch.object(service, "_schedule", side_effect=AssertionError("scheduled")):
                    service.set_appearance("annotation")
                    self.assertEqual(service.get_appearance(), {"skin": "annotation"})
                    self.assertEqual(service.get_settings()["model"], "chosen")
                    after = service._snapshot(panel, "thread-a")
                    for key in ("revision", "suggestions", "usage", "recent_jobs", "status"):
                        self.assertEqual(before[key], after[key])
                    self.assertEqual(after["appearance"], {"skin": "annotation"})
            finally:
                service.shutdown()
