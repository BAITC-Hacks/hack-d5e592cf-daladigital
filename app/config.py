"""Keep mutable data and large models outside cloud-synchronised source folders."""

import os
import sys
from pathlib import Path


def _default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "AI-Protokolist"
    return Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "ai-protokolist"


APP_HOME = Path(os.getenv("PROTOCOL_HOME", str(_default_home()))).expanduser().resolve()
DATA_DIR = Path(os.getenv("PROTOCOL_DATA_DIR", str(APP_HOME / "data"))).expanduser().resolve()
MODEL_DIR = Path(os.getenv("PROTOCOL_MODEL_DIR", str(APP_HOME / "models"))).expanduser().resolve()
