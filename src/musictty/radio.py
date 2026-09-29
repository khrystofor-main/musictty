"""The radio: keeps mpv's playlist full of similar music. Runs in the background process.

A port of v0's youtube-music.lua. mpv's playlist is the queue:
- when fewer than a few tracks are left, the current track's mix (RDAMVM<id>) is fetched
  and the tracks not played yet are appended as ytdl:// entries;
- the next entry is swapped in advance for a direct audio stream, so switching tracks
  never waits for yt-dlp;
- played entries beyond a limit are trimmed, so the playlist doesn't grow.
The track list for `musictty list` (and later the TUI) is published in user-data/musictty/list.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
SOURCE_PROPERTY = "user-data/musictty/source"
JUMP_MESSAGE = "musictty-jump"

RADIO_MIX = "radio mix"
LIKED_PLAYLIST = "liked playlist"


@dataclass
class LaunchSpec:
    """What the background process should play; the CLI passes it as JSON on stdin."""

    seed: str
    stream: Stream | None = None  # the seed already resolved by the CLI, if that worked
    queue: list[Track] | None = None  # a fixed playlist (liked tracks), seed first; None = radio
    source: str = RADIO_MIX  # shown by `musictty now`
    volume: int = 70
    loop_file: bool = False

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
        )


class Source(Protocol):
    """Where tracks come from: the youtube module, or a fake in tests."""

    def resolve(self, video_id: str) -> Stream | None: ...
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
        # the liked playlist: no mixes, nothing trimmed, mpv loops it (--loop-playlist)
        self.fixed = spec.queue is not None
        self.seen: set[str] = set()  # tracks that were queued or played: never queue them again
        self.fetching = False
        self.direct_id: dict[str, str] = {}  # direct stream URL -> track id
        self.resolving: set[str] = set()
        self.names: dict[str, str] = {}  # track id -> "Artist — Title"
        # entries that have played: after going back, `list` still shows the ones "ahead"
        self.played: set[int] = set()
        self.seed_recorded = self.fixed  # the liked playlist is not a radio: no seed history
        self.last_logged: str | None = None
        # the entry being loaded, from start-file. Events come in order but may be handled
        # after the user has already moved on: file-loaded is about this entry, not the current
        self.loading: int | None = None
        self.edit = asyncio.Lock()  # playlist edits that depend on indexes must not interleave
        self.tasks: set[asyncio.Task] = set()
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
        if entry_id in self.played:
            self.played.discard(entry_id)
            self.played.add(new_id)
        if self.loading == entry_id:
            self.loading = new_id
        return new_id

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
        while (event := await self.mpv.next_event()) is not None:
            try:
                await self.handle(event)
            except MpvError as e:
                log.debug("mpv: %s", e)
            except Exception:
                log.exception("failed to handle %s", event.get("event"))
        for task in self.tasks:
            task.cancel()
        self.pool.shutdown(wait=False, cancel_futures=True)

    async def handle(self, event: dict) -> None:
        match event.get("event"):
            case "start-file":
                self.loading = event.get("playlist_entry_id")
            case "file-loaded":
                await self.on_file_loaded()
            case "end-file" if event.get("reason") == "error":
                await self.on_load_error(event.get("playlist_entry_id"))
            case "client-message":
                await self.on_message(event.get("args") or [])

    async def start(self) -> None:
        spec = self.spec
        await self.mpv.set(SOURCE_PROPERTY, spec.source)
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
            for track in spec.queue[1:]:
                await self.loadfile(track_url(track.id), "append")
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
        video_id = self.video_id(entry.get("filename"))
        if video_id:
            self.seen.add(video_id)

        if not self.fixed:
            # trim the tail of played entries and top up the queue
            left = len(playlist) - pos - 1
            extra = pos - KEEP_BEHIND
            if extra > 0:
                async with self.edit:
                    for gone in playlist[:extra]:
                        self.direct_id.pop(gone.get("filename"), None)
                        self.played.discard(gone.get("id"))
                        await self.mpv.command("playlist-remove", "0")
            if left < REFILL_WHEN_LEFT:
                self.spawn(self.refill(video_id))

        await self.prefetch_next()
        if video_id:
            if video_id not in self.names:
                self.names[video_id] = await self.mpv.get("media-title") or video_id
            self.log_play(video_id, self.names[video_id])
        await self.publish_list()

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
                if not self.fixed:
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
            stream = await self.blocking(self.yt.resolve, video_id)
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
        if not self.fixed and playlist and (pos is None or pos >= len(playlist) - 1):
            self.spawn(self.refill(self.video_id(playlist[-1].get("filename"))))

    async def on_message(self, args: list[str]) -> None:
        # `musictty list back N`: go back to an entry, keeping the queue after it
        if len(args) == 2 and args[0] == JUMP_MESSAGE:
            try:
                entry_id = int(args[1])
            except ValueError:
                return
            at = _index(await self.playlist(), entry_id)
            if at is not None:
                await self.mpv.command("playlist-play-index", str(at))

    async def publish_list(self) -> None:
        """Played entries and the current one, in queue order.

        entry is mpv's permanent entry id: it doesn't shift when old entries are trimmed.
        """
        playlist = await self.playlist()
        pos = _current(playlist)
        if pos is None:
            return
        items = []
        for i, entry in enumerate(playlist):
            if i <= pos or entry.get("id") in self.played:
                video_id = self.video_id(entry.get("filename"))
                title = self.names.get(video_id) if video_id else None
                if not title and i == pos:
                    title = await self.mpv.get("media-title")
                items.append(
                    {
                        "entry": entry.get("id"),
                        "id": video_id or "",
                        "title": title or video_id or "?",
                        "current": i == pos,
                    }
                )
        await self.mpv.set(LIST_PROPERTY, items)
