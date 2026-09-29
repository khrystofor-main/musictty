import os
import tempfile

import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own data folder and IPC address per test: the real player and data stay untouched."""
    monkeypatch.setenv("MUSICTTY_HOME", str(tmp_path / "home"))
    if os.name == "nt":
        address = rf"\\.\pipe\musictty-test-{os.getpid()}-{id(tmp_path)}"
    else:
        # unix socket paths are limited to ~104 bytes: keep it short
        address = os.path.join(tempfile.mkdtemp(prefix="mt"), "mpv.sock")
    monkeypatch.setenv("MUSICTTY_IPC", address)
    return tmp_path / "home"
