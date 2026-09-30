"""YouTube Music's catalogue through ytmusicapi: search, albums, artists' top songs.

yt-dlp still plays everything and builds the mixes; its search only gives bare ids for
albums and artists, while ytmusicapi returns titles, artists and years in one request.
Every call blocks on the network: async code runs it in a thread. ytmusicapi is imported
lazily, like yt_dlp.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .models import Track
from .youtube import display_title, is_video_id

SONGS, ALBUMS, ARTISTS = "song", "album", "artist"


@dataclass(frozen=True)
class Result:
    kind: str  # SONGS, ALBUMS or ARTISTS
    id: str  # video id, album browse id or artist channel id
    title: str  # "Artist — Title" for songs and albums, the name for artists
    detail: str = ""  # duration of a song, "Album · 1995" of an album


@dataclass
class Found:
    songs: list[Result] = field(default_factory=list)
    albums: list[Result] = field(default_factory=list)
    artists: list[Result] = field(default_factory=list)


def _client() -> Any:
    from ytmusicapi import YTMusic

    return YTMusic()


def _names(artists: Any) -> str:
    return ", ".join(a["name"] for a in artists or [] if isinstance(a, dict) and a.get("name"))


def _song(item: dict) -> Result | None:
    video_id = item.get("videoId") or ""
    if not is_video_id(video_id) or item.get("isAvailable") is False:
        return None
    title = display_title(_names(item.get("artists")), item.get("title") or video_id)
    return Result(SONGS, video_id, title, item.get("duration") or "")


def _album(item: dict) -> Result | None:
    browse_id = item.get("browseId")
    if not browse_id or not item.get("title"):
        return None
    artist = _names(item.get("artists")) or item.get("artist") or ""
    detail = " · ".join(str(x) for x in (item.get("type") or "Album", item.get("year")) if x)
    return Result(ALBUMS, browse_id, display_title(artist, item["title"]), detail)


def _artist(item: dict) -> Result | None:
    browse_id = item.get("browseId")
    name = item.get("artist") or item.get("title")
    return Result(ARTISTS, browse_id, name) if browse_id and name else None


def search(query: str, limit: int = 8) -> Found:
    """Songs, albums and artists for a query: three requests at once."""
    kinds = (("songs", _song), ("albums", _album), ("artists", _artist))

    def one(name: str) -> list:
        # a client per thread: its requests session is not meant to be shared
        return _client().search(query, filter=name, limit=limit)

    with ThreadPoolExecutor(max_workers=len(kinds)) as pool:
        futures = [pool.submit(one, name) for name, _ in kinds]
        found = Found()
        for (name, parse), future in zip(kinds, futures, strict=True):
            results = [r for r in map(parse, future.result() or []) if r][:limit]
            setattr(found, name, results)
    return found


def album(browse_id: str) -> tuple[str, list[Track]]:
    """An album's title and its playable tracks, in order."""
    data = _client().get_album(browse_id)
    tracks = [Track(s.id, s.title) for s in map(_song, data.get("tracks") or []) if s]
    return display_title(_names(data.get("artists")), data.get("title") or ""), tracks


def artist_songs(browse_id: str) -> tuple[str, list[Result]]:
    """An artist's name and top songs."""
    data = _client().get_artist(browse_id)
    songs = [s for s in map(_song, (data.get("songs") or {}).get("results") or []) if s]
    return data.get("name") or "", songs
