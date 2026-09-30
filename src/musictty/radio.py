"""The radio: keeps mpv's playlist full of similar music. Runs in the background process.

A port of v0's youtube-music.lua. mpv's playlist is the queue:
- when fewer than a few tracks are left, the current track's mix (RDAMVM<id>) is fetched
  and the tracks not played yet are appended as ytdl:// entries;
- the next entry is swapped in advance for a direct audio stream, so switching tracks
  never waits for yt-dlp;
- played entries beyond a limit are trimmed, so the playlist doesn't grow.
The track list for `musictty list` and the UI is published in user-data/musictty/list, the
tracks still to play in user-data/musictty/upnext.

The radio is the only one that edits the playlist: the UI and the CLI ask it to through
script-messages (add, remove, move, shuffle), naming entries by their mpv entry id.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any, Protocol, TypeVar

from . import youtube
from .ipc import Mpv, MpvError
from .models import Track
from .store import Store
from .youtube import Stream

log = logging.getLogger(__name__)

REFILL_WHEN_LEFT = 3  # tracks that must stay queued
MIX_SIZE = 25  # tracks taken from one mix
KEEP_BEHIND = 30  # played entries kept for "prev" (an entry costs ~1 KB)
PREFETCH_AHEAD = 1  # upcoming tracks resolved to direct streams in advance

T = TypeVar("T")

LIST_PROPERTY = "user-data/musictty/list"
UPNEXT_PROPERTY = "user-data/musictty/upnext"
SOURCE_PROPERTY = "user-data/musictty/source"
PID_PROPERTY = "user-data/musictty/pid"  # the radio process, for `musictty mem`
JUMP_MESSAGE = "musictty-jump"  # play an entry where it is: back in the radio's list
PLAY_MESSAGE = "musictty-play"  # play an upcoming entry now, keeping the ones before it
ADD_MESSAGE = "musictty-add"  # "next" | "queue", then [[id, title], ...] as JSON
REMOVE_MESSAGE = "musictty-remove"
MOVE_MESSAGE = "musictty-move"  # entry, "before" | "after", target entry
SHUFFLE_MESSAGE = "musictty-shuffle"
SLEEP_MESSAGE = "musictty-sleep"  # seconds until the radio stops (0: never), or SLEEP_END
SLEEP_END = "end"  # stop after the track that's playing
# "" (no timer), SLEEP_END, or the time the radio stops (seconds since the epoch)
SLEEP_PROPERTY = "user-data/musictty/sleep"
FADE_SECONDS = 30.0  # the sleep timer turns the volume down over the last half minute
DISLIKE_MESSAGE = "musictty-dislike"  # a video id: skip it, never pick it again
QUALITY_MESSAGE = "musictty-quality"  # one of youtube.QUALITIES, for the tracks to come
PLAY_NEXT, ADD_TO_QUEUE = "next", "queue"

RADIO_MIX = "radio mix"
LIKED_PLAYLIST = "liked playlist"
ALBUM = "album"
AI_RADIO = "ai radio"
ARTIST_RADIO = "artist radio"
PLAYLIST = "playlist"


@dataclass
class LaunchSpec:
    """What the background process should play; the CLI passes it as JSON on stdin."""

    seed: str
    stream: Stream | None = None  # the seed already resolved by the CLI, if that worked
    queue: list[Track] | None = None  # a fixed playlist (liked tracks), seed first; None = radio
    source: str = RADIO_MIX  # shown by `musictty now`
    volume: int = 70
    loop_file: bool = False
    # the queue plays once, then the radio goes on from its last track (an album)
    then_radio: bool = False
    quality: str = youtube.DEFAULT_QUALITY
    # the sleep timer of the radio this one replaces, as SLEEP_PROPERTY: it goes on
    sleep: str = ""

    @property
    def loops(self) -> bool:
        """A fixed playlist that starts over at the end: the liked tracks."""
        return self.queue is not None and not self.then_radio

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> LaunchSpec:
        data = json.loads(text)
        stream = data.get("stream")
        queue = data.get("queue")
        return cls(
            seed=data["seed"],
            stream=Stream(**stream) if stream else None,
            queue=[Track(**t) for t in queue] if queue is not None else None,
            source=data.get("source") or RADIO_MIX,
            volume=int(data.get("volume", 70)),
            loop_file=bool(data.get("loop_file", False)),
            then_radio=bool(data.get("then_radio", False)),
            quality=data.get("quality") or youtube.DEFAULT_QUALITY,
            sleep=str(data.get("sleep") or ""),
        )


class Source(Protocol):
    """Where tracks come from: the youtube module, or a fake in tests."""

    def resolve(self, video_id: str, fmt: str) -> Stream | None: ...
    def mix(self, seed: str, limit: int) -> list[Track]: ...
    def check(self, stream: Stream) -> tuple[bool, str]: ...


def track_url(video_id: str) -> str:
    # ytdl:// sends mpv straight to yt-dlp, without trying to open the HTML page itself
    return "ytdl://" + youtube.watch_url(video_id)


def file_options(**options: str) -> str:
    """Per-file options for loadfile. %length% quoting keeps commas and '=' in values intact."""
    return ",".join(f"{k}=%{len(v.encode())}%{v}" for k, v in options.items() if v)


def _current(playlist: list[dict]) -> int | None:
    return next((i for i, e in enumerate(playlist) if e.get("current")), None)


def _index(playlist: list[dict], entry_id: int | None) -> int | None:
    return next((i for i, e in enumerate(playlist) if e.get("id") == entry_id), None)


class Radio:
    def __init__(
        self, mpv: Mpv, spec: LaunchSpec, store: Store | None = None, source: Source = youtube
    ):
        self.mpv = mpv
        self.spec = spec
        self.store = store or Store()
        self.yt = source
        self.format = youtube.FORMATS.get(spec.quality, youtube.FORMAT)
        # repeat all (mpv's loop-playlist; the liked playlist starts with it): no mixes,
        # nothing trimmed. Followed as it changes
        self.looping = spec.loops
        # a queue (an album, the liked playlist): its tracks in order, the mixes only once
        # it has played out (see `queued`)
        self.album = {t.id for t in spec.queue} if spec.queue else set()
        # the queue proper, as opposed to the radio's own tracks: the entries of a playlist or
        # an album and the ones the user added, until they play. "Add to queue" goes after them
        self.queued: set[int] = set()
        # tracks that were queued or played, and the disliked ones: never queue them again
        self.seen: set[str] = set(self.store.disliked_ids())
        self.fetching = False
        self.direct_id: dict[str, str] = {}  # direct stream URL -> track id
        self.resolving: set[str] = set()
        self.names: dict[str, str] = {}  # track id -> "Artist — Title"
        # entries that have played: after going back, `list` still shows the ones "ahead"
        self.played: set[int] = set()
        # a playlist or an album is not a radio: no seed history
        self.seed_recorded = spec.queue is not None
        self.last_logged: str | None = None
        # the entry being loaded, from start-file. Events come in order but may be handled
        # after the user has already moved on: file-loaded is about this entry, not the current
        self.loading: int | None = None
        self.edit = asyncio.Lock()  # playlist edits that depend on indexes must not interleave
        self.tasks: set[asyncio.Task] = set()
        # the sleep timer: a task that stops the radio, or stop at the end of this track
        self.sleep_task: asyncio.Task | None = None
        self.sleep_at_end = False
        # network calls block: they run here. Own pool, so quitting never waits for them
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="musictty")

    # --- plumbing ---

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(self._guard(coro))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _guard(self, coro: Coroutine[Any, Any, Any]) -> None:
        try:
            await coro
        except MpvError as e:
            log.debug("mpv: %s", e)
        except Exception:
            log.exception("background task failed")

    async def blocking(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    async def settle(self) -> None:
        """Wait until background work (mix fetches, prefetches) is done."""
        while self.tasks:
            await asyncio.gather(*self.tasks)

    def video_id(self, url: str | None) -> str | None:
        if not url:
            return None
        return self.direct_id.get(url) or youtube.video_id_from_url(url)

    async def playlist(self) -> list[dict]:
        return await self.mpv.get("playlist") or []

    async def loadfile(
        self, url: str, flags: str, title: str = "", user_agent: str = ""
    ) -> int | None:
        cmd: dict[str, Any] = {"name": "loadfile", "url": url, "flags": flags}
        options = file_options(**{"force-media-title": title, "user-agent": user_agent})
        if options:
            cmd["options"] = options
        reply = await self.mpv.command(cmd)
        return reply.get("playlist_entry_id") if isinstance(reply, dict) else None

    async def replace_entry(
        self, entry_id: int, url: str, title: str = "", user_agent: str = ""
    ) -> int | None:
        """Swap an entry for another URL in place, keeping its "played" mark. Returns the new id."""
        async with self.edit:
            new_id = await self.loadfile(url, "append", title, user_agent)
            playlist = await self.playlist()
            new_at = _index(playlist, new_id)
            if new_at is None:  # an mpv that doesn't report the id: the entry is the last such URL
                matches = [i for i, e in enumerate(playlist) if e.get("filename") == url]
                if not matches:
                    return None
                new_at = matches[-1]
                new_id = playlist[new_at].get("id")
            old_at = _index(playlist, entry_id)
            if old_at is None:  # it's gone meanwhile
                await self.mpv.command("playlist-remove", str(new_at))
                return None
            await self.mpv.command("playlist-move", str(new_at), str(old_at))
            await self.mpv.command("playlist-remove", str(old_at + 1))
        for marks in (self.played, self.queued):
            if entry_id in marks:
                marks.discard(entry_id)
                marks.add(new_id)
        if self.loading == entry_id:
            self.loading = new_id
        return new_id

    def forget(self, gone: dict, rest: list[dict]) -> None:
        """A removed entry's stream URL, unless the same track is queued with it again."""
        url = gone.get("filename")
        if all(e.get("filename") != url for e in rest):
            self.direct_id.pop(url, None)

    def record_seed(self, title: str) -> None:
        self.seed_recorded = True
        try:
            self.store.add_seed(Track(self.spec.seed, title))
        except OSError as e:
            log.warning("could not save the radio seed: %s", e)

    def log_play(self, video_id: str, title: str) -> None:
        # the same track twice in a row is not logged (repeat gives no file-loaded anyway)
        if video_id == self.last_logged:
            return
        self.last_logged = video_id
        try:
            self.store.add_play(Track(video_id, title or video_id))
        except OSError as e:
            log.warning("could not save the play history: %s", e)

    # --- the radio ---

    async def run(self) -> None:
        """Start playing and serve mpv's events until it quits."""
        await self.start()
        await self.mpv.command("observe_property", 1, "loop-playlist")
        while (event := await self.mpv.next_event()) is not None:
            try:
                await self.handle(event)
            except MpvError as e:
                log.debug("mpv: %s", e)
            except Exception:
                log.exception("failed to handle %s", event.get("event"))
        for task in self.tasks:
            task.cancel()
        if self.sleep_task:
            self.sleep_task.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def handle(self, event: dict) -> None:
        match event.get("event"):
            case "start-file":
                self.loading = event.get("playlist_entry_id")
            case "file-loaded":
                await self.on_file_loaded()
            case "end-file" if event.get("reason") == "eof" and self.sleep_at_end:
                log.info("sleep timer: the track is over, stopping")
                await self.mpv.command("quit")
            case "end-file" if event.get("reason") == "error":
                await self.on_load_error(event.get("playlist_entry_id"))
            case "client-message":
                await self.on_message(event.get("args") or [])
            case "property-change" if event.get("name") == "loop-playlist":
                await self.on_loop_changed(event.get("data") not in (None, False, "no"))

    async def start(self) -> None:
        spec = self.spec
        await self.mpv.set(SOURCE_PROPERTY, spec.source)
        await self.mpv.set(PID_PROPERTY, os.getpid())
        await self.carry_sleep(spec.sleep)
        for track in spec.queue or []:
            self.names[track.id] = track.title
        if spec.stream:
            # the CLI already resolved the first track: play the direct stream right away
            self.direct_id[spec.stream.url] = spec.seed
            self.names.setdefault(spec.seed, spec.stream.title)
            await self.loadfile(
                spec.stream.url, "replace", self.names[spec.seed], spec.stream.user_agent
            )
        else:
            # it didn't work in advance: let mpv ask yt-dlp itself
            await self.loadfile(track_url(spec.seed), "replace")
        if spec.queue is not None:
            self.seen.update(self.album)  # the radio after an album won't repeat it
            for track in spec.queue[1:]:
                entry_id = await self.loadfile(track_url(track.id), "append")
                if entry_id is not None:
                    self.queued.add(entry_id)
            await self.prefetch_next()
            await self.publish_list()
        else:
            # start fetching the mix at once, without waiting for the first track to load
            self.seen.add(spec.seed)
            self.spawn(self.refill(spec.seed))

    async def on_file_loaded(self) -> None:
        playlist = await self.playlist()
        pos = _current(playlist) if self.loading is None else _index(playlist, self.loading)
        if pos is None:
            return
        entry = playlist[pos]
        self.played.add(entry.get("id"))
        from_queue = entry.get("id") in self.queued
        self.queued.discard(entry.get("id"))
        video_id = self.video_id(entry.get("filename"))
        if video_id:
            self.seen.add(video_id)

        if not self.looping:
            # trim the tail of played entries and top up the queue
            extra = max(0, pos - KEEP_BEHIND)
            if extra:
                async with self.edit:
                    for gone in playlist[:extra]:
                        self.forget(gone, playlist[extra:])
                        self.played.discard(gone.get("id"))
                        await self.mpv.command("playlist-remove", "0")
                playlist, pos = playlist[extra:], pos - extra
            self.top_up(playlist, pos, video_id)
            if self.album and video_id and video_id not in self.album and not from_queue:
                # past the album: from here on it's a radio
                self.album = set()
                await self.mpv.set(SOURCE_PROPERTY, RADIO_MIX)

        await self.prefetch_next()
        if video_id:
            if video_id not in self.names:
                self.names[video_id] = await self.mpv.get("media-title") or video_id
            self.log_play(video_id, self.names[video_id])
        await self.publish_list()

    def top_up(self, playlist: list[dict], pos: int, seed: str | None) -> None:
        """Fetch a mix when few tracks are left, once the queue proper has played out."""
        ahead = playlist[pos + 1 :]
        if self.looping or len(ahead) >= REFILL_WHEN_LEFT:
            return
        if not any(e.get("id") in self.queued for e in ahead):
            self.spawn(self.refill(seed))

    async def refill(self, seed: str | None) -> None:
        if self.fetching or not seed:
            return
        self.fetching = True
        log.info("fetching the mix of %s", seed)
        try:
            tracks = await self.blocking(self.yt.mix, seed, MIX_SIZE)
        except Exception as e:
            log.warning("could not fetch the mix: %s", e)
            return
        finally:
            self.fetching = False
        added = 0
        for track in tracks:
            # mix data is the most accurate (it has the artist): it wins over the player's title
            self.names[track.id] = track.title
            if track.id == self.spec.seed and not self.seed_recorded:
                self.record_seed(track.title)
            if track.id not in self.seen:
                self.seen.add(track.id)
                async with self.edit:  # not in the middle of a queue edit
                    await self.loadfile(track_url(track.id), "append-play")
                added += 1
        log.info("tracks added: %d", added)
        if not self.seed_recorded:
            # the seed wasn't in its own mix: use the title we have
            title = self.names.get(self.spec.seed) or await self.mpv.get("media-title")
            self.record_seed(title or self.spec.seed)
        if added == 0:
            # everything in the mix has played: hook on to the last track of the queue
            playlist = await self.playlist()
            other = self.video_id(playlist[-1].get("filename")) if playlist else None
            if other and other != seed:
                await self.refill(other)
        await self.prefetch_next()
        await self.publish_list()

    async def prefetch_next(self) -> None:
        playlist = await self.playlist()
        pos = _current(playlist)
        if pos is None:
            return
        for step in range(1, PREFETCH_AHEAD + 1):
            i = pos + step
            if i >= len(playlist):
                if not self.looping:
                    break
                i %= len(playlist)  # the liked playlist loops
                if i == pos:
                    break
            entry = playlist[i]
            self.spawn(self.prefetch(entry.get("id"), entry.get("filename") or ""))

    async def prefetch(self, entry_id: int, filename: str, attempt: int = 1) -> None:
        """Swap a ytdl:// entry for a direct stream obtained in advance."""
        if not filename.startswith("ytdl://"):
            return
        video_id = self.video_id(filename)
        if not video_id or (video_id in self.resolving and attempt == 1):
            return
        self.resolving.add(video_id)
        try:
            stream = await self.blocking(self.yt.resolve, video_id, self.format)
            if stream is None:
                return  # the ytdl:// entry stays and works the usual way
            good, code = await self.blocking(self.yt.check, stream)
            if not good:
                log.warning("stream answered %s, attempt %d: %s", code, attempt, video_id)
                if attempt < 2:
                    await self.prefetch(entry_id, filename, attempt + 1)
                return
            # the user may have skipped while we waited: look the entry up again
            playlist = await self.playlist()
            at = _index(playlist, entry_id)
            if at is None or at == _current(playlist):
                return
            title = self.names.setdefault(video_id, stream.title)
            self.direct_id[stream.url] = video_id
            await self.replace_entry(entry_id, stream.url, title, stream.user_agent)
            log.info("ready in advance: %s", title)
            await self.publish_list()  # the entry has a new id
        finally:
            self.resolving.discard(video_id)

    async def on_load_error(self, entry_id: int | None) -> None:
        """A track didn't open (removed, region-locked, stale link): don't let the radio die."""
        playlist = await self.playlist()
        at = _index(playlist, entry_id)
        filename = playlist[at].get("filename", "") if at is not None else ""
        video_id = self.video_id(filename)
        if video_id and not filename.startswith("ytdl://"):
            # a direct stream expired (~6 h, matters for "prev") or was refused: put the
            # ytdl:// entry back and play it. No loop: ytdl:// entries never get here.
            log.warning("direct stream did not open, retrying through yt-dlp: %s", video_id)
            fixed = track_url(video_id)
            new_id = await self.replace_entry(entry_id, fixed)
            # after an error mpv steps to the next track itself: come back after its step
            await asyncio.sleep(0.05)
            playlist = await self.playlist()
            at = _index(playlist, new_id)
            if at is not None and at != _current(playlist):
                await self.mpv.command("playlist-play-index", str(at))
            return
        pos = _current(playlist)
        # in the liked playlist the end is fine: --loop-playlist goes back to the start
        if not self.looping and playlist and (pos is None or pos >= len(playlist) - 1):
            self.spawn(self.refill(self.video_id(playlist[-1].get("filename"))))

    async def on_message(self, args: list[str]) -> None:
        match args:
            case [name, entry] if name in (JUMP_MESSAGE, PLAY_MESSAGE) and entry.isdigit():
                await self.play_entry(int(entry), keep_before=name == PLAY_MESSAGE)
            case [name, where, tracks] if name == ADD_MESSAGE:
                try:
                    items = [Track(str(i), str(t)) for i, t in json.loads(tracks)]
                except (ValueError, TypeError):
                    return
                await self.add(items, next_up=where == PLAY_NEXT)
            case [name, entry] if name == REMOVE_MESSAGE and entry.isdigit():
                await self.remove(int(entry))
            case [name, entry, where, target] if (
                name == MOVE_MESSAGE and entry.isdigit() and target.isdigit()
            ):
                await self.move(int(entry), int(target), after=where == "after")
            case [name] if name == SHUFFLE_MESSAGE:
                await self.shuffle()
            case [name, when] if name == SLEEP_MESSAGE:
                await self.set_sleep(when)
            case [name, video_id] if name == DISLIKE_MESSAGE:
                await self.dislike(video_id)
            case [name, quality] if name == QUALITY_MESSAGE and quality in youtube.FORMATS:
                self.format = youtube.FORMATS[quality]
                await self.mpv.set("ytdl-format", self.format)
                log.info("audio quality: %s", quality)

    async def play_entry(self, entry_id: int, keep_before: bool) -> None:
        """`list back N` plays an entry where it is (the queue after it is kept). From up next,
        an entry is moved up to play now, so the ones before it still come next."""
        async with self.edit:
            playlist = await self.playlist()
            at, pos = _index(playlist, entry_id), _current(playlist)
            if at is None or at == pos:
                return
            if keep_before and pos is not None and at > pos + 1:
                await self.mpv.command("playlist-move", str(at), str(pos + 1))
                at = pos + 1
            await self.mpv.command("playlist-play-index", str(at))

    async def add(self, tracks: list[Track], next_up: bool) -> None:
        """Play next: right after the current track. Add to queue: after what the user queued
        before, ahead of the radio's own tracks."""
        if not tracks:
            return
        async with self.edit:
            playlist = await self.playlist()
            pos = _current(playlist)
            if pos is None:  # the queue has run out: play them
                at = None
            elif next_up:
                at = pos + 1
            else:
                at = pos + 1
                for i in range(pos + 1, len(playlist)):
                    if playlist[i].get("id") in self.queued:
                        at = i + 1
            placed = 0
            for track in tracks:
                self.names[track.id] = track.title
                self.seen.add(track.id)
                url = track_url(track.id)
                entry_id = await self.loadfile(url, "append" if at is not None else "append-play")
                if at is None:
                    if entry_id is not None:
                        self.queued.add(entry_id)
                    continue
                # found by its id: something else may have been appended meanwhile
                playlist = await self.playlist()
                i = _index(playlist, entry_id) if entry_id is not None else len(playlist) - 1
                if i is None:
                    continue
                self.queued.add(playlist[i].get("id"))
                await self.mpv.command("playlist-move", str(i), str(at + placed))
                placed += 1
        log.info("queued %d tracks (%s)", len(tracks), PLAY_NEXT if next_up else ADD_TO_QUEUE)
        await self.after_edit()

    async def remove(self, entry_id: int) -> None:
        async with self.edit:
            playlist = await self.playlist()
            at = _index(playlist, entry_id)
            if at is None or at == _current(playlist):  # the playing track stays
                return
            await self.mpv.command("playlist-remove", str(at))
            self.forget(playlist[at], playlist[:at] + playlist[at + 1 :])
            self.queued.discard(entry_id)
            self.played.discard(entry_id)
        await self.after_edit()

    async def move(self, entry_id: int, target_id: int, after: bool) -> None:
        """Put an upcoming entry just before or after another one."""
        async with self.edit:
            playlist = await self.playlist()
            at, to = _index(playlist, entry_id), _index(playlist, target_id)
            if at is None or to is None or _current(playlist) in (at, to) or at == to:
                return
            # mpv puts the entry in the target's place, shifting the target down
            await self.mpv.command("playlist-move", str(at), str(to + 1 if after else to))
            # moved in among the queue, it's part of it; among the radio's tracks, one of them
            playlist = await self.playlist()
            at = _index(playlist, entry_id)
            if at is not None:
                before = playlist[at - 1] if at else {}
                if before.get("current") or before.get("id") in self.queued:
                    self.queued.add(entry_id)
                else:
                    self.queued.discard(entry_id)
        await self.after_edit()

    async def shuffle(self) -> None:
        """Shuffle what's up next; the played tracks and the current one stay as they are."""
        async with self.edit:
            playlist = await self.playlist()
            pos = _current(playlist)
            if pos is None:
                return
            ids = [e.get("id") for e in playlist]
            upcoming = self.upcoming(len(ids), pos)
            order = random.sample(upcoming, len(upcoming))
            # the target order, built front to back with mpv's moves, mirrored in `ids`
            wanted = [ids[i] for i in range(pos + 1)] if not self.looping else [ids[pos]]
            wanted += [ids[i] for i in order]
            for k, entry_id in enumerate(wanted):
                i = ids.index(entry_id)
                if i != k:
                    await self.mpv.command("playlist-move", str(i), str(k))
                    ids.insert(k, ids.pop(i))
            self.queued.clear()  # the shuffled queue is one queue now
        log.info("shuffled %d tracks", len(upcoming))
        await self.after_edit()

    async def dislike(self, video_id: str) -> None:
        """Skip the track if it's playing, and take it out of the radio's picks ahead; the
        mixes won't bring it back. What the user queued stays: they asked for it."""
        self.seen.add(video_id)
        async with self.edit:
            playlist = await self.playlist()
            pos = _current(playlist)
            if pos is None:
                return
            for i in reversed(range(pos + 1, len(playlist))):
                entry = playlist[i]
                gone = self.video_id(entry.get("filename")) == video_id
                if gone and entry.get("id") not in self.queued:
                    await self.mpv.command("playlist-remove", str(i))
                    self.forget(entry, playlist[:i] + playlist[i + 1 :])
            skip = self.video_id(playlist[pos].get("filename")) == video_id
        log.info("disliked %s", video_id)
        if skip:
            await self.mpv.command("playlist-next", "force")
        await self.after_edit()

    async def carry_sleep(self, value: str) -> None:
        """The timer of the radio this one replaced: until the same time, or after a track."""
        if value == SLEEP_END:
            await self.set_sleep(SLEEP_END)
            return
        try:
            left = float(value) - time.time()
        except ValueError:
            return
        if left > 0:
            await self.set_sleep(str(left))

    async def set_sleep(self, when: str) -> None:
        """A new sleep timer replaces the one set before."""
        if self.sleep_task:
            self.sleep_task.cancel()
            self.sleep_task = None
        self.sleep_at_end = when == SLEEP_END
        if self.sleep_at_end:
            await self.mpv.set(SLEEP_PROPERTY, SLEEP_END)
            log.info("sleep timer: after this track")
            return
        try:
            seconds = float(when)
        except ValueError:
            seconds = 0
        if seconds <= 0:
            await self.mpv.set(SLEEP_PROPERTY, "")
            log.info("sleep timer off")
            return
        await self.mpv.set(SLEEP_PROPERTY, str(time.time() + seconds))
        self.sleep_task = asyncio.create_task(self.sleep(seconds))
        log.info("sleep timer: %d min", round(seconds / 60))

    async def sleep(self, seconds: float) -> None:
        """Wait, turn the volume down, stop. Cancelled (a new timer, off): the volume comes back."""
        fade = min(FADE_SECONDS, seconds)
        volume = None
        try:
            await asyncio.sleep(seconds - fade)
            volume = await self.mpv.get("volume")
            steps = max(10, int(fade))  # a step a second, at least ten
            for step in range(1, steps + 1):
                if volume is not None:
                    await self.mpv.set("volume", volume * (1 - step / steps))
                await asyncio.sleep(fade / steps)
            log.info("sleep timer: time's up, stopping")
            await self.mpv.command("quit")
        except asyncio.CancelledError:
            if volume is not None:
                with contextlib.suppress(MpvError):
                    await self.mpv.set("volume", volume)
            raise
        except MpvError:
            pass  # the player is gone already

    async def after_edit(self) -> None:
        await self.prefetch_next()
        await self.publish_list()

    async def on_loop_changed(self, looping: bool) -> None:
        if looping == self.looping:
            return
        self.looping = looping
        log.info("repeat all %s", "on" if looping else "off")
        if not looping:
            # the queue may be at its end already: the radio goes on from there
            playlist = await self.playlist()
            pos = _current(playlist)
            if pos is not None:
                self.top_up(playlist, pos, self.video_id(playlist[-1].get("filename")))
        await self.after_edit()

    def upcoming(self, count: int, pos: int) -> list[int]:
        """Indexes of the entries still to play, in order; with repeat all, round to the current."""
        ahead = list(range(pos + 1, count))
        return ahead + list(range(pos)) if self.looping else ahead

    def item(self, entry: dict, fallback: str | None = None) -> dict:
        video_id = self.video_id(entry.get("filename"))
        title = (self.names.get(video_id) if video_id else None) or fallback or video_id or "?"
        return {"entry": entry.get("id"), "id": video_id or "", "title": title}

    async def publish_list(self) -> None:
        """The radio's list: played entries and the current one, in queue order. Up next: the
        entries still to play.

        entry is mpv's permanent entry id: it doesn't shift when old entries are trimmed.
        """
        playlist = await self.playlist()
        pos = _current(playlist)
        if pos is None:
            return
        items = []
        for i, entry in enumerate(playlist):
            if i <= pos or entry.get("id") in self.played:
                item = self.item(entry)
                if i == pos and item["title"] in (item["id"], "?"):  # not named yet
                    item = self.item(entry, await self.mpv.get("media-title"))
                items.append({**item, "current": i == pos})
        upnext = [
            {**self.item(playlist[i]), "queued": playlist[i].get("id") in self.queued}
            for i in self.upcoming(len(playlist), pos)
        ]
        await self.mpv.set(LIST_PROPERTY, items)
        await self.mpv.set(UPNEXT_PROPERTY, upnext)
