"""Listening data: plain UTF-8 text, one record per line: date <TAB> id <TAB> title.

- seeds.tsv   tracks radios were started from (the bare `musictty` list)
- plays.tsv   every started track of every radio (`musictty history`)
- liked.tsv   liked tracks (`musictty like / liked`)
- settings.json  volume and repeat, shared by all radios
- playlists.json  your own playlists: [{"name": ..., "tracks": [[id, title], ...]}, ...]

The same line format as v0, so its files can be imported as they are.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import paths
from .models import Track

SEEDS = "seeds.tsv"
PLAYS = "plays.tsv"
LIKED = "liked.tsv"
SETTINGS = "settings.json"
PLAYLISTS = "playlists.json"

DEFAULT_VOLUME = 70
MAX_VOLUME = 130  # mpv's own limit


@dataclass
class Playlist:
    name: str
    tracks: list[Track]


class PlaylistError(Exception):
    """Something to tell the user: a name taken, a playlist gone."""


@dataclass
class Settings:
    volume: int = DEFAULT_VOLUME
    repeat: bool = False


def _clean(text: str) -> str:
    return re.sub(r"[\t\r\n]", " ", text)


def _line(track: Track, stamp: str | None = None) -> str:
    stamp = stamp or datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"{stamp}\t{_clean(track.id)}\t{_clean(track.title)}"


def read_records(path: Path) -> list[tuple[str, Track]]:
    """(date, track) per valid line; a missing file is an empty list."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    records = []
    for raw in text.split("\n"):
        date, _, rest = raw.rstrip("\r").partition("\t")
        track_id, _, title = rest.partition("\t")
        if track_id:
            records.append((date, Track(track_id, title or track_id)))
    return records


def write_lines(path: Path, lines: list[str]) -> None:
    """Replace the file atomically, so a crash never leaves it half-written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(line + "\n" for line in lines)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else paths.data_dir()

    def path(self, name: str) -> Path:
        return self.root / name

    def _records(self, name: str) -> list[tuple[str, Track]]:
        return read_records(self.path(name))

    def _append(self, name: str, track: Track) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.path(name), "a", encoding="utf-8", newline="\n") as f:
            f.write(_line(track) + "\n")

    # --- radio seeds ---

    def add_seed(self, track: Track) -> None:
        self._append(SEEDS, track)

    def recent_seeds(self, limit: int = 10) -> list[Track]:
        """Latest distinct seeds, newest first."""
        seen: set[str] = set()
        out = []
        for _, track in reversed(self._records(SEEDS)):
            if track.id not in seen:
                seen.add(track.id)
                out.append(track)
                if len(out) == limit:
                    break
        return out

    # --- play history ---

    def add_play(self, track: Track) -> None:
        self._append(PLAYS, track)

    def recent_plays(self, limit: int = 30) -> list[Track]:
        """Latest plays, newest first; a track played twice is listed twice."""
        return [track for _, track in reversed(self._records(PLAYS)[-limit:])]

    def trim_plays(self, max_bytes: int = 100 * 1024, keep: int = 300) -> None:
        """The play log grows with every track: now and then cut it to the latest lines."""
        path = self.path(PLAYS)
        try:
            if path.stat().st_size <= max_bytes:
                return
        except FileNotFoundError:
            return
        write_lines(path, [_line(t, d) for d, t in self._records(PLAYS)[-keep:]])

    # --- likes ---

    def liked(self) -> list[Track]:
        """Liked tracks in the order they were liked, without repeats.

        YouTube ids are case-sensitive, so they are compared exactly.
        """
        seen: set[str] = set()
        out = []
        for _, track in self._records(LIKED):
            if track.id not in seen:
                seen.add(track.id)
                out.append(track)
        return out

    def liked_ids(self) -> set[str]:
        return {track.id for track in self.liked()}

    def like(self, track: Track) -> bool:
        if track.id in self.liked_ids():
            return False
        self._append(LIKED, track)
        return True

    def unlike(self, track_id: str) -> bool:
        records = self._records(LIKED)
        keep = [_line(t, d) for d, t in records if t.id != track_id]
        if len(keep) == len(records):
            return False
        write_lines(self.path(LIKED), keep)
        return True

    # --- settings ---

    def settings(self) -> Settings:
        try:
            data = json.loads(self.path(SETTINGS).read_text(encoding="utf-8"))
            volume = max(0, min(MAX_VOLUME, int(data.get("volume", DEFAULT_VOLUME))))
            return Settings(volume=volume, repeat=bool(data.get("repeat", False)))
        except (OSError, ValueError, TypeError, AttributeError):
            return Settings()

    def save_settings(self, settings: Settings) -> None:
        write_lines(self.path(SETTINGS), [json.dumps(asdict(settings))])

    # --- playlists ---

    def playlists(self) -> list[Playlist]:
        """Your playlists, in the order they were made."""
        try:
            data = json.loads(self.path(PLAYLISTS).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        out = []
        for item in data if isinstance(data, list) else []:
            try:
                tracks = [Track(str(i), str(t)) for i, t in item.get("tracks") or []]
                out.append(Playlist(str(item["name"]), tracks))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue  # a hand-edited entry that makes no sense
        return out

    def _save_playlists(self, playlists: list[Playlist]) -> None:
        data = [{"name": p.name, "tracks": [[t.id, t.title] for t in p.tracks]} for p in playlists]
        write_lines(self.path(PLAYLISTS), [json.dumps(data, ensure_ascii=False, indent=1)])

    def _find(self, playlists: list[Playlist], name: str) -> Playlist:
        found = next((p for p in playlists if p.name == name), None)
        if found is None:
            raise PlaylistError(f"no playlist «{name}»")
        return found

    def create_playlist(self, name: str) -> Playlist:
        name = " ".join(name.split())
        if not name:
            raise PlaylistError("a playlist needs a name")
        playlists = self.playlists()
        if any(p.name.casefold() == name.casefold() for p in playlists):
            raise PlaylistError(f"«{name}» already exists")
        playlist = Playlist(name, [])
        self._save_playlists([*playlists, playlist])
        return playlist

    def delete_playlist(self, name: str) -> None:
        playlists = self.playlists()
        self._find(playlists, name)
        self._save_playlists([p for p in playlists if p.name != name])

    def add_to_playlist(self, name: str, tracks: list[Track]) -> int:
        """Add tracks at the end; ones already there are skipped. Returns how many were added."""
        playlists = self.playlists()
        playlist = self._find(playlists, name)
        new: dict[str, Track] = {t.id: t for t in playlist.tracks}
        count = len(new)
        for track in tracks:
            new.setdefault(track.id, track)
        playlist.tracks = list(new.values())
        self._save_playlists(playlists)
        return len(new) - count

    def remove_from_playlist(self, name: str, track_id: str) -> None:
        playlists = self.playlists()
        playlist = self._find(playlists, name)
        playlist.tracks = [t for t in playlist.tracks if t.id != track_id]
        self._save_playlists(playlists)

    def move_in_playlist(self, name: str, track_id: str, step: int) -> None:
        playlists = self.playlists()
        tracks = self._find(playlists, name).tracks
        at = next((i for i, t in enumerate(tracks) if t.id == track_id), None)
        if at is None or not 0 <= at + step < len(tracks):
            return
        tracks.insert(at + step, tracks.pop(at))
        self._save_playlists(playlists)


# --- import from v0 (the PowerShell version) ---

V0_MARKER = ".imported-v0"
V0_FILES = {"history.txt": SEEDS, "play-history.txt": PLAYS}


class AlreadyImported(Exception):
    pass


def find_v0_dir() -> Path | None:
    """v0 lives in a folder on PATH, next to radio.ps1."""
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        folder = Path(entry) if entry else None
        if folder and (folder / "radio.ps1").is_file() and (folder / "youtube-music.lua").is_file():
            return folder
    return None


def import_v0(store: Store, src: Path) -> dict[str, int]:
    """Merge v0's text files into the store, once. v0 records are older, so they go first.

    Returns how many records were taken per file (SETTINGS: 1 if volume/repeat were taken).
    Nothing found means nothing is marked, so a wrong folder can be corrected.
    """
    marker = store.path(V0_MARKER)
    if marker.exists():
        raise AlreadyImported(marker)
    counts: dict[str, int] = {}

    for v0_name, name in V0_FILES.items():
        old = read_records(src / v0_name)
        if old:
            merged = old + store._records(name)
            write_lines(store.path(name), [_line(t, d) for d, t in merged])
            counts[name] = len(old)

    old_liked = read_records(src / "liked.txt")
    if old_liked:
        seen: set[str] = set()
        merged = []
        for date, track in old_liked + store._records(LIKED):
            if track.id not in seen:
                seen.add(track.id)
                merged.append(_line(track, date))
        write_lines(store.path(LIKED), merged)
        counts[LIKED] = len({t.id for _, t in old_liked})

    # settings only if the new version has none of its own yet
    if not store.path(SETTINGS).exists():
        settings = Settings(repeat=(src / "repeat-on").exists())
        try:
            text = (src / "volume.txt").read_text(encoding="utf-8-sig").strip()
            settings.volume = max(0, min(MAX_VOLUME, int(text)))
        except (OSError, ValueError):
            pass
        if settings != Settings():
            store.save_settings(settings)
            counts[SETTINGS] = 1

    if counts:
        marker.write_text(str(src), encoding="utf-8")
    return counts
