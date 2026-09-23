"""Keep mutable data and large models outside cloud-synchronised source folders."""

import os
import sys
from pathlib import Path


def _default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AI-Protokolist"
    return Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ai-protokolist"


# A fresh application opens the isolated demonstration automatically, regardless of launcher.
os.environ.setdefault("PROTOCOL_DEMO_MODE", "1")

APP_HOME = Path(os.getenv("PROTOCOL_HOME", str(_default_home()))).expanduser().resolve()
DEFAULT_DATA_DIR = APP_HOME / ("demo-data" if os.getenv("PROTOCOL_DEMO_MODE") == "1" else "data")
DATA_DIR = (DEFAULT_DATA_DIR if os.getenv("PROTOCOL_DEMO_MODE") == "1"
            else Path(os.getenv("PROTOCOL_DATA_DIR", str(DEFAULT_DATA_DIR)))).expanduser().resolve()
MODEL_DIR = Path(os.getenv("PROTOCOL_MODEL_DIR", str(APP_HOME / "models"))).expanduser().resolve()
