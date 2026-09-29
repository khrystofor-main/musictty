"""Where musictty keeps its data and where the player listens for commands."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import platformdirs

APP = "musictty"


def data_dir() -> Path:
    """Listening data and settings. MUSICTTY_HOME overrides it (tests, portable setups)."""
    home = os.environ.get("MUSICTTY_HOME")
    return Path(home) if home else Path(platformdirs.user_data_dir(APP, appauthor=False))


def ipc_address() -> str:
    """mpv's JSON IPC endpoint: a named pipe on Windows, a unix socket elsewhere.

    MUSICTTY_IPC overrides it, so tests never talk to a real player.
    """
    override = os.environ.get("MUSICTTY_IPC")
    if override:
        return override
    if sys.platform == "win32":
        return r"\\.\pipe\musictty"
    home = os.environ.get("MUSICTTY_HOME")
    base = Path(home) if home else Path(platformdirs.user_runtime_dir(APP, appauthor=False))
    return str(base / "mpv.sock")


def log_path() -> Path:
    """Log of the background radio process, rewritten on every start."""
    return data_dir() / "radio.log"
