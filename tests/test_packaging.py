import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("next_packager", ROOT / "scripts/package.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


class PackagingTests(unittest.TestCase):
    def test_portable_and_legacy_commands_survive_packaging_and_zip(self):
        # Agent Plugins rejects absolute commands; legacy MCP can keep this local path.
        interpreter = r"C:\Python Runtime With Spaces\python.exe"
        with tempfile.TemporaryDirectory(prefix="next-package-test-") as folder:
            destination = Path(folder) / "local-plugin"
            with patch.object(packager.sys, "executable", interpreter):
                archive = packager.build(destination)
            portable = json.loads((destination / "mcp.json").read_text(encoding="utf-8"))
            legacy = json.loads((destination / ".mcp.json").read_text(encoding="utf-8"))
            portable_server = portable["mcpServers"]["mogoo-codex-next"]
            self.assertEqual(portable_server["type"], "stdio")
            self.assertEqual(portable_server["command"], "python")
            self.assertEqual(portable_server["args"], ["${PLUGIN_ROOT}/scripts/run.py", "serve"])
            self.assertEqual(legacy["mcpServers"]["mogoo-codex-next"]["command"], interpreter)
            with zipfile.ZipFile(archive) as bundle:
                zipped_portable = json.loads(bundle.read("local-plugin/mcp.json"))
                zipped_legacy = json.loads(bundle.read("local-plugin/.mcp.json"))
            self.assertEqual(zipped_portable, portable)
            self.assertEqual(zipped_legacy, legacy)
