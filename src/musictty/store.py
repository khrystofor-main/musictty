"""Listening data: plain UTF-8 text, one record per line: date <TAB> id <TAB> title.

- seeds.tsv   tracks radios were started from (the bare `musictty` list)
- plays.tsv   every started track of every radio (`musictty history`)
- liked.tsv   liked tracks (`musictty like / liked`)
- settings.json  volume and repeat, shared by all radios

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

DEFAULT_VOLUME = 70
MAX_VOLUME = 130  # mpv's own limit


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
