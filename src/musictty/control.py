"""Actions on the radio, shared by the command line and the terminal UI."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TypeVar

from . import paths, player, youtube
from .ipc import Mpv, NotRunning
from .models import Track
from .radio import ALBUM, LIKED_PLAYLIST, LIST_PROPERTY, RADIO_MIX, LaunchSpec
from .store import MAX_VOLUME, Store

T = TypeVar("T")


class Failure(Exception):
    """Something to tell the user: the CLI prints it, the UI shows it."""


async def with_player(fn: Callable[[Mpv], Awaitable[T]]) -> T:
    """Run fn against the playing radio; NotRunning if there is none."""
    mpv = await Mpv.connect(paths.ipc_address())
    try:
        return await fn(mpv)
    finally:
        await mpv.close()


async def current_track(mpv: Mpv) -> dict | None:
    items = await mpv.get(LIST_PROPERTY) or []
    return next((it for it in items if it.get("current")), None)


async def repeating(mpv: Mpv) -> bool:
    return await mpv.get("loop-file") not in (None, False, "no")


def list_lines(
    items: list[dict], repeat: bool, liked: set[str], numbered: bool = True
) -> list[str]:
    """The radio's tracks: ▶ current, ↻ at the current one when repeating, ♥ liked.

    The ↻ / ♥ columns appear only when someone needs them; blanks keep titles aligned.
    """
    any_liked = any(it.get("id") in liked for it in items)
    lines = []
    for n, it in enumerate(items, 1):
        mark = "▶ " if it.get("current") else "  "
        if repeat:
            mark += "↻ " if it.get("current") else "  "
        if any_liked:
            mark += "♥ " if it.get("id") in liked else "  "
        lines.append((f"{n:2}. " if numbered else "") + mark + it.get("title", "?"))
    return lines


def liked_queue(items: list[Track], index: int) -> list[Track]:
    """From the chosen track to the end, then the top: in a loop that's "from here down"."""
    return items[index:] + items[:index]


async def start(
    seed: str | None = None,
    *,
    query: str | None = None,
    queue: list[Track] | None = None,
    repeat_one: bool = False,
    album: bool = False,
) -> None:
    """Start a new radio, replacing the playing one.

    With a queue: the liked playlist in a loop, or an album followed by a radio.
    """
    if not player.find_mpv():
        raise Failure("mpv not found")
    if query is not None:
        found = await asyncio.to_thread(youtube.search, query)
        if not found:
            raise Failure("nothing found")
        seed, stream = found
    else:
        assert seed
        stream = await asyncio.to_thread(youtube.resolve, seed)
    store = Store()
    settings = store.settings()
    spec = LaunchSpec(
        seed=seed,
        stream=stream,
        queue=queue,
        source=ALBUM if album else LIKED_PLAYLIST if queue is not None else RADIO_MIX,
        then_radio=album,
        volume=settings.volume,
        # repeat is global and survives radio changes; repeat_one is for this run only
        loop_file=settings.repeat or repeat_one,
    )
    address = paths.ipc_address()
    # the old radio kept playing while the new one was being prepared: stop it only now
    await player.stop(address)
    store.trim_plays()
    proc = player.spawn_radio(spec, paths.log_path())
    try:
        mpv = await player.wait_connect(address, lambda: proc.poll() is None, timeout=15)
    except NotRunning:
        raise Failure(f"the player did not start, see {paths.log_path()}") from None
    await mpv.close()


async def stop() -> None:
    await player.stop(paths.ipc_address())


async def set_repeat(on: bool) -> None:
    # remember first (works with the radio off too), then apply to the playing one
    store = Store()
    store.save_settings(replace(store.settings(), repeat=on))
    with contextlib.suppress(NotRunning):
        await with_player(lambda mpv: mpv.set("loop-file", "inf" if on else "no"))


async def change_volume(delta: int) -> int:
    """Change the playing radio and remember; with the radio off, only the remembered value."""
    store = Store()
    settings = store.settings()

    async def step(mpv: Mpv) -> int:
        await mpv.command("add", "volume", delta)
        return round(await mpv.get("volume", settings.volume))

    try:
        volume = await with_player(step)
    except NotRunning:
        volume = max(0, min(MAX_VOLUME, settings.volume + delta))
    store.save_settings(replace(settings, volume=volume))
    return volume
