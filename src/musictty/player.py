"""The player processes: finding mpv, starting the background radio, stopping it."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
from collections.abc import Callable
from pathlib import Path

from . import youtube
from .ipc import Mpv, MpvError, NotRunning
from .radio import LaunchSpec

MPV_CONF = Path(__file__).with_name("mpv.conf")
WINDOWS = sys.platform == "win32"


class PlayerError(Exception):
    pass


def find_mpv() -> str | None:
    if not WINDOWS:
        return shutil.which("mpv")
    # exactly mpv.exe: plain "mpv" finds the mpv.com wrapper, which hangs around as an extra process
    candidates = [
        shutil.which("mpv.exe"),
        os.path.expandvars(r"%ProgramFiles%\MPV Player\mpv.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
    ]
    return next((c for c in candidates if c and os.path.isfile(c)), None)


def find_ytdlp() -> str | None:
    """yt-dlp for mpv's ytdl hook (ytdl:// entries); the one installed with musictty first."""
    name = "yt-dlp.exe" if WINDOWS else "yt-dlp"
    local = Path(sysconfig.get_path("scripts")) / name
    return str(local) if local.is_file() else shutil.which("yt-dlp")


def mpv_command(mpv: str, address: str, spec: LaunchSpec) -> list[str]:
    args = [
        mpv,
        "--no-config",
        f"--include={MPV_CONF}",
        f"--input-ipc-server={address}",
        "--idle=yes",  # the radio loads the first track itself, once it's connected
        f"--ytdl-format={youtube.FORMAT}",
        f"--volume={spec.volume}",
    ]
    if spec.loop_file:
        args.append("--loop-file=inf")
    if spec.queue is not None:
        args.append("--loop-playlist=inf")  # the liked playlist starts over at the end
    ytdlp = find_ytdlp()
    if ytdlp:
        args.append(f"--script-opts-append=ytdl_hook-ytdl_path={ytdlp}")
    # extra mpv options, e.g. --ao=null in tests or an audio device
    args += shlex.split(os.environ.get("MUSICTTY_MPV_ARGS", ""))
    return args


def _clear_stale(address: str) -> None:
    """mpv leaves its unix socket file behind when it exits."""
    if WINDOWS:
        return
    path = Path(address)
    try:
        if stat.S_ISSOCK(path.stat().st_mode):
            path.unlink()
    except FileNotFoundError:
        pass


def launch_mpv(spec: LaunchSpec, address: str) -> subprocess.Popen:
    mpv = find_mpv()
    if not mpv:
        raise PlayerError("mpv not found")
    if not WINDOWS:
        Path(address).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _clear_stale(address)
    flags = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
    # stdout/stderr are inherited: they go to the radio log
    return subprocess.Popen(
        mpv_command(mpv, address, spec), stdin=subprocess.DEVNULL, creationflags=flags
    )


def spawn_radio(spec: LaunchSpec, log_path: Path) -> subprocess.Popen:
    """Start the background radio (python -m musictty.daemon), detached from the terminal."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {}
    if WINDOWS:
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "musictty.daemon"],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    assert proc.stdin
    proc.stdin.write(spec.to_json().encode())
    proc.stdin.close()
    return proc


async def wait_connect(address: str, alive: Callable[[], bool], timeout: float = 10.0) -> Mpv:
    """Connect to a player that is starting up."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            return await Mpv.connect(address, timeout=0.5)
        except NotRunning:
            if not alive() or loop.time() > deadline:
                raise
            await asyncio.sleep(0.05)


async def is_running(address: str) -> bool:
    try:
        mpv = await Mpv.connect(address)
    except NotRunning:
        return False
    await mpv.close()
    return True


async def _gone(address: str, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while await is_running(address):
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


async def stop(address: str) -> None:
    """Stop the radio for sure: politely over IPC, and by force if mpv hangs."""
    try:
        mpv = await Mpv.connect(address)
    except NotRunning:
        _clear_stale(address)
        return
    pid = None
    try:
        pid = await mpv.command("get_property", "pid", timeout=1)
        await mpv.command("quit", timeout=1)
    except MpvError:
        pass
    finally:
        await mpv.close()
    if not await _gone(address, 1.5) and pid:
        with contextlib.suppress(OSError):
            os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        await _gone(address, 2)
    _clear_stale(address)
