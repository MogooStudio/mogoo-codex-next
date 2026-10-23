"""Display-only preference, deliberately separate from inference configuration."""
import json
import os
from pathlib import Path
import tempfile
import threading

SKINS = ("graphite", "annotation", "focus")
_LOCK = threading.RLock()


def load_appearance(root: Path) -> dict:
    path = Path(root) / "appearance.json"
    with _LOCK:
        try:
            if not path.exists():
                return {"skin": "graphite"}
            if path.stat().st_size > 4096:
                raise ValueError()
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {"skin"} or value["skin"] not in SKINS:
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError):
            return {"skin": "graphite", "error": "无法读取外观设置，请重新选择皮肤。"}


def save_appearance(root: Path, skin: str) -> dict:
    if not isinstance(skin, str) or skin not in SKINS:
        raise ValueError("请选择石墨、批注或聚焦皮肤")
    value = {"skin": skin}
    with _LOCK:
        root = Path(root)
        filename = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            fd, filename = tempfile.mkstemp(prefix=".appearance-", dir=root)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(filename, root / "appearance.json")
        except OSError:
            raise ValueError("外观没有保存，请重试。") from None
        finally:
            try:
                if filename and os.path.exists(filename):
                    os.unlink(filename)
            except OSError:
                pass
    return value
