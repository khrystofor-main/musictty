"""Actions on the radio, shared by the command line and the terminal UI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any, TypeVar

from . import ai, music, paths, player, youtube
from .ipc import Mpv, NotRunning
from .models import Track
from .radio import (
    ADD_MESSAGE,
    AI_RADIO,
    DISLIKE_MESSAGE,
    LIKED_PLAYLIST,
    LIST_PROPERTY,
    QUALITY_MESSAGE,
    RADIO_MIX,
    SLEEP_END,
    SLEEP_MESSAGE,
    SLEEP_PROPERTY,
    UPNEXT_PROPERTY,
    LaunchSpec,
)
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


async def repeating_all(mpv: Mpv) -> bool:
    return await mpv.get("loop-playlist") not in (None, False, "no")


async def upnext(mpv: Mpv) -> list[dict]:
    return await mpv.get(UPNEXT_PROPERTY) or []


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
    then_radio: bool = False,
    source: str | None = None,
) -> None:
    """Start a new radio, replacing the playing one.

    With a queue: the liked playlist in a loop, or (then_radio) a queue that turns into a
    radio from its last track: an album, the AI's picks.
    """
    if not player.find_mpv():
        raise Failure("mpv not found")
    store = Store()
    settings = store.settings()
    fmt = youtube.FORMATS[settings.quality]
    if query is not None:
        found = await asyncio.to_thread(youtube.search, query, fmt)
        if not found:
            raise Failure("nothing found")
        seed, stream = found
    else:
        assert seed
        stream = await asyncio.to_thread(youtube.resolve, seed, fmt)
    spec = LaunchSpec(
        seed=seed,
        stream=stream,
        queue=queue,
        source=source or (LIKED_PLAYLIST if queue is not None else RADIO_MIX),
        then_radio=then_radio,
        volume=settings.volume,
        quality=settings.quality,
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


async def ai_radio(mood: str) -> list[Track]:
    """Ask the AI for songs that fit the mood, play them, then a radio from the last one."""
    try:
        queries = await asyncio.to_thread(ai.suggest, mood)
    except ai.AIError as e:
        raise Failure(str(e)) from None
    tracks = await asyncio.to_thread(music.find_songs, queries)
    if not tracks:
        raise Failure("none of the ai's songs were found")
    await start(tracks[0].id, queue=tracks, then_radio=True, source=AI_RADIO)
    return tracks


async def enqueue(tracks: list[Track], where: str, source: str | None = None) -> bool:
    """Play next / add to queue on the playing radio. With the radio off they just play: one
    track as a radio, several (an album) in order and then a radio. True if they were queued.
    """
    if not tracks:
        return False
    items = json.dumps([[t.id, t.title] for t in tracks], ensure_ascii=False)
    try:
        await with_player(lambda mpv: mpv.command("script-message", ADD_MESSAGE, where, items))
        return True
    except NotRunning:
        pass
    if len(tracks) == 1:
        await start(tracks[0].id)
    else:
        await start(tracks[0].id, queue=tracks, then_radio=True, source=source)
    return False


async def message(*args: str) -> None:
    """Ask the radio to do something with its queue (see radio.Radio.on_message)."""
    await with_player(lambda mpv: mpv.command("script-message", *args))


async def set_repeat_all(on: bool) -> None:
    """Repeat the whole queue. Only for the playing one: a new radio starts without it."""
    await with_player(lambda mpv: mpv.set("loop-playlist", "inf" if on else "no"))


async def dislike(track: Track) -> None:
    """Never again: remember it, and have the playing radio skip it and drop it."""
    Store().dislike(track)
    with contextlib.suppress(NotRunning):
        await message(DISLIKE_MESSAGE, track.id)


async def set_sleep(when: str) -> None:
    """Stop the playing radio in `when` seconds ("0": no timer), or at the end of the track."""
    await message(SLEEP_MESSAGE, when)


def sleep_text(value: Any, now: float) -> str:
    """The sleep timer as the radio publishes it, for people: "" if there is none."""
    if value == SLEEP_END:
        return "sleep after this track"
    try:
        left = float(value) - now
    except (TypeError, ValueError):
        return ""
    return f"sleep {int(left // 60)}:{int(left % 60):02}" if left > 0 else ""


async def sleep_status(mpv: Mpv) -> str:
    return sleep_text(await mpv.get(SLEEP_PROPERTY), time.time())


async def set_quality(quality: str) -> None:
    """Remember the audio quality; the playing radio takes it for the tracks to come."""
    store = Store()
    store.save_settings(replace(store.settings(), quality=quality))
    with contextlib.suppress(NotRunning):
        await message(QUALITY_MESSAGE, quality)


async def seek(seconds: int) -> None:
    await with_player(lambda mpv: mpv.command("seek", seconds, "relative"))


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
