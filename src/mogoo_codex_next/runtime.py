"""Load plugin code while its versioned installation directory still exists."""
from importlib import import_module
from pathlib import Path
import sys


def preload_modules():
    # Enumerate once at MCP startup, including future plugin modules. Runtime
    # imports then resolve from sys.modules even if Codex removes this version.
    failed = False
    try:
        modules = sorted(Path(__file__).parent.glob("*.py"))
    except OSError:
        modules = []
        failed = True
    for path in modules:
        if path.stem == "__init__":
            continue
        try:
            import_module("mogoo_codex_next." + path.stem)
        except Exception:
            # Keep already loaded modules usable and try the remaining ones.
            # Import failures may contain local paths or provider information.
            failed = True
    if failed:
        print("Mogoo Codex Next: some modules could not be preloaded.", file=sys.stderr)
    return not failed
