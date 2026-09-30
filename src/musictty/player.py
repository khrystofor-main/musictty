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
        f"--ytdl-format={youtube.FORMATS.get(spec.quality, youtube.FORMAT)}",
        f"--volume={spec.volume}",
    ]
    if spec.loop_file:
        args.append("--loop-file=inf")
    if spec.loops:
        args.append("--loop-playlist=inf")  # the liked playlist starts over at the end
    ytdlp = find_ytdlp()
    if ytdlp:
        args.append(f"--script-opts-append=ytdl_hook-ytdl_path={ytdlp}")
    # extra mpv options, e.g. --ao=null in tests or an audio device
    args += shlex.split(os.environ.get("MUSICTTY_MPV_ARGS", ""))
    return args


def memory(pid: int) -> tuple[int | None, int] | None:
    """(private, working set) bytes of a process; private is None where the OS doesn't say."""
    try:
        if WINDOWS:
            return _windows_memory(pid)
        if sys.platform.startswith("linux"):
            fields = {}
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                key, _, value = line.partition(":")
                if value.strip().endswith("kB"):
                    fields[key] = int(value.split()[0]) * 1024
            return fields.get("RssAnon"), fields["VmRSS"]
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
        return None, int(out.stdout.strip()) * 1024
    except (OSError, ValueError, KeyError):
        return None


def _windows_memory(pid: int) -> tuple[int | None, int] | None:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS_EX
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            *(
                (name, ctypes.c_size_t)
                for name in (
                    "PeakWorkingSetSize",
                    "WorkingSetSize",
                    "QuotaPeakPagedPoolUsage",
                    "QuotaPagedPoolUsage",
                    "QuotaPeakNonPagedPoolUsage",
                    "QuotaNonPagedPoolUsage",
                    "PagefileUsage",
                    "PeakPagefileUsage",
                    "PrivateUsage",
                )
            ),  # fmt: skip
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        counters = Counters(cb=ctypes.sizeof(Counters))
        if not kernel32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None
        return counters.PrivateUsage, counters.WorkingSetSize
    finally:
        kernel32.CloseHandle(handle)


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


LOG_LIMIT = 512 * 1024


def _open_log(path: Path):
    """The previous radio may still write its last lines: append, never truncate under it.

    Once the log is big, it moves to radio.log.1 and a new one starts (on Windows that waits
    until no process holds the old one).
    """
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass
    return open(path, "ab")


def spawn_background(args: list[str], stdin: bytes, log_path: Path) -> subprocess.Popen:
    """Start a process detached from the terminal: stdin from `stdin`, output to the log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict = {}
    if WINDOWS:
        # a hidden console, not none at all (DETACHED_PROCESS): the venv's python.exe is a
        # launcher that starts the real interpreter as a child, and a child of a process
        # without a console opens a console window of its own
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with _open_log(log_path) as log:
        proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, **kwargs
        )
    assert proc.stdin
    proc.stdin.write(stdin)
    proc.stdin.close()
    return proc


def spawn_radio(spec: LaunchSpec, log_path: Path) -> subprocess.Popen:
    """Start the background radio (python -m musictty.daemon)."""
    args = [sys.executable, "-m", "musictty.daemon"]
    return spawn_background(args, spec.to_json().encode(), log_path)


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
