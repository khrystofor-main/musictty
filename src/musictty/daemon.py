"""The background radio process: python -m musictty.daemon, with a LaunchSpec as JSON on stdin.

It starts a hidden mpv, connects to its IPC and keeps the radio going until mpv quits
(`musictty stop`, or a new radio replacing this one).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from . import paths, player
from .radio import LaunchSpec, Radio

log = logging.getLogger("musictty")


async def serve(spec: LaunchSpec) -> int:
    address = paths.ipc_address()
    try:
        proc = player.launch_mpv(spec, address)
    except (player.PlayerError, OSError) as e:
        log.error("could not start mpv: %s", e)
        return 1
    failed = False
    try:
        mpv = await player.wait_connect(address, lambda: proc.poll() is None)
        try:
            await Radio(mpv, spec).run()
        finally:
            await mpv.close()
    except Exception:
        log.exception("the radio stopped")
        failed = True
    finally:
        if failed and proc.poll() is None:
            proc.terminate()  # no radio without us: don't leave a player that never refills
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 1 if failed else 0


def main() -> None:
    # the log is a file: UTF-8 whatever the locale (titles come in every script)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    spec = LaunchSpec.from_json(sys.stdin.buffer.read().decode("utf-8"))
    code = asyncio.run(serve(spec))
    # a yt-dlp request may still be running in a worker thread: don't wait for it
    logging.shutdown()
    os._exit(code)


if __name__ == "__main__":
    main()
