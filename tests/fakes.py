"""In-memory stand-ins for mpv and YouTube, enough to exercise the radio logic."""

from __future__ import annotations

import asyncio
import re

from musictty.ipc import MpvError
from musictty.models import Track
from musictty.youtube import Stream


def tid(n: int) -> str:
    return f"track{n:06d}"  # 11 characters, like a YouTube id


def parse_options(text: str) -> dict[str, str]:
    """mpv's key=%len%value,... list (lengths in bytes)."""
    out, rest = {}, text.encode()
    while rest:
        key, _, rest = rest.partition(b"=")
        m = re.match(rb"%(\d+)%", rest)
        assert m, text
        n = int(m.group(1))
        value, rest = rest[m.end() : m.end() + n], rest[m.end() + n :].lstrip(b",")
        out[key.decode()] = value.decode()
    return out


class FakeMpv:
    """mpv's playlist semantics as far as the radio uses them."""

    def __init__(self):
        self.entries: list[dict] = []  # {"id", "filename", "title"?}
        self.current_id: int | None = None
        self.props: dict = {}
        self.next_id = 1
        self.log: list = []
        self.closed = False
        self.quit = False
        self.events: asyncio.Queue = asyncio.Queue()

    # helpers for tests
    @property
    def pos(self) -> int | None:
        return next((i for i, e in enumerate(self.entries) if e["id"] == self.current_id), None)

    def filenames(self) -> list[str]:
        return [e["filename"] for e in self.entries]

    def play(self, index: int) -> None:
        self.current_id = self.entries[index]["id"]

    # the Mpv interface
    async def command(self, *args, timeout=None):
        cmd = args[0] if len(args) == 1 and isinstance(args[0], dict) else list(args)
        self.log.append(cmd)
        if isinstance(cmd, dict):
            assert cmd["name"] == "loadfile"
            return self._loadfile(cmd["url"], cmd["flags"], cmd.get("options", ""))
        name, *rest = cmd
        if name == "get_property":
            return self._get(rest[0])
        if name == "set_property":
            self.props[rest[0]] = rest[1]
        elif name == "playlist-remove":
            removed = self.entries.pop(int(rest[0]))
            if removed["id"] == self.current_id:
                self.current_id = None
        elif name == "playlist-move":
            i, j = int(rest[0]), int(rest[1])
            entry = self.entries.pop(i)
            self.entries.insert(j - 1 if i < j else j, entry)
        elif name == "playlist-play-index":
            self.play(int(rest[0]))
        elif name == "observe_property":
            pass  # the tests send the property-change events themselves
        elif name == "quit":
            self.quit = True
        else:
            raise AssertionError(f"unexpected command {cmd}")
        return None

    def _loadfile(self, url: str, flags: str, options: str) -> dict:
        entry = {"id": self.next_id, "filename": url}
        self.next_id += 1
        title = parse_options(options).get("force-media-title") if options else None
        if title:
            entry["title"] = title
        if flags == "replace":
            self.entries = [entry]
            self.current_id = entry["id"]
        else:
            self.entries.append(entry)
            if flags == "append-play" and self.current_id is None:
                self.current_id = entry["id"]
        return {"playlist_entry_id": entry["id"]}

    def _get(self, name: str):
        if name == "playlist":
            return [
                {**e, **({"current": True} if e["id"] == self.current_id else {})}
                for e in self.entries
            ]
        if name == "media-title":
            pos = self.pos
            if pos is None:
                raise MpvError("property unavailable")
            return self.entries[pos].get("title") or self.entries[pos]["filename"]
        if name in self.props:
            return self.props[name]
        raise MpvError("property unavailable")

    async def get(self, name, default=None, timeout=None):
        try:
            return await self.command("get_property", name)
        except MpvError:
            return default

    async def set(self, name, value):
        await self.command("set_property", name, value)

    async def next_event(self):
        return await self.events.get()


class FakeSource:
    def __init__(self, mixes: dict[str, list[str]] | None = None, bad: set[str] = frozenset()):
        self.mixes = mixes or {}
        self.bad = bad  # ids whose streams answer 403
        self.resolved: list[str] = []
        self.formats: list[str | None] = []
        self.mixed: list[str] = []

    def title(self, video_id: str) -> str:
        return f"Artist — {video_id}"

    def stream(self, video_id: str) -> Stream:
        return Stream(video_id, f"player title {video_id}", f"https://stream/{video_id}", "UA")

    def resolve(self, video_id, fmt=None):
        self.resolved.append(video_id)
        self.formats.append(fmt)
        return self.stream(video_id)

    def mix(self, seed, limit):
        self.mixed.append(seed)
        return [Track(i, self.title(i)) for i in self.mixes.get(seed, [])][:limit]

    def check(self, stream):
        return (False, "403") if stream.id in self.bad else (True, "206")
