"""YouTube Music's catalogue through ytmusicapi: search, albums, artists' pages.

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
from .radio import ALBUM as ALBUM_SOURCE
from .youtube import display_title, is_video_id

SONGS, ALBUMS, ARTISTS = "song", "album", "artist"
RADIO = "radio"  # an artist's radio: id is its playlist id
MORE_SONGS = "more songs"  # all of an artist's songs: id is the playlist's browse id
MORE_ALBUMS = "more albums"  # all albums or singles: id is the channel id, with params


@dataclass(frozen=True)
class Result:
    kind: str  # SONGS, ALBUMS, ARTISTS; RADIO, MORE_SONGS, MORE_ALBUMS on an artist's page
    id: str  # video id, album browse id or artist channel id
    title: str  # "Artist — Title" for songs and albums, the name for artists
    detail: str = ""  # duration of a song, "Album · 1995" of an album
    params: str = ""  # what ytmusicapi needs besides the id (MORE_ALBUMS)


@dataclass
class Section:
    title: str  # "" for none
    results: list[Result]
    numbered: bool = False  # an album's tracks


@dataclass
class Page:
    """An artist, an album or a longer list of an artist's songs or albums."""

    title: str
    detail: str = ""
    sections: list[Section] = field(default_factory=list)
    # an album or a list of songs: enter on a song plays them from there, under this label
    plays_as: str = ""
    artist: str = ""  # an album's artist, left out of its track titles


@dataclass(frozen=True)
class Lyrics:
    lines: list[tuple[float | None, str]]  # (start in seconds when timed, text)
    timed: bool
    source: str = ""  # "Source: LyricFind"


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


def _song(item: dict, artist: str = "") -> Result | None:
    video_id = item.get("videoId") or ""
    if not is_video_id(video_id) or item.get("isAvailable") is False:
        return None
    artists = _names(item.get("artists")) or item.get("artist") or artist
    title = display_title(artists, item.get("title") or video_id)
    # the watch playlist (an artist's radio) calls the duration "length"
    return Result(SONGS, video_id, title, item.get("duration") or item.get("length") or "")


def _album(item: dict, artist: str = "") -> Result | None:
    browse_id = item.get("browseId")
    if not browse_id or not item.get("title"):
        return None
    # albums on an artist's page come without the artist
    artist = _names(item.get("artists")) or item.get("artist") or artist
    detail = " · ".join(str(x) for x in (item.get("type") or "Album", item.get("year")) if x)
    return Result(ALBUMS, browse_id, display_title(artist, item["title"]), detail)


def _artist(item: dict) -> Result | None:
    browse_id = item.get("browseId")
    name = item.get("artist") or item.get("title")
    if not browse_id or not name:
        return None
    subscribers = item.get("subscribers")
    return Result(ARTISTS, browse_id, name, f"{subscribers} subscribers" if subscribers else "")


def _parsed(items: Any, parse: Any, *args: Any) -> list[Result]:
    return [r for r in (parse(item, *args) for item in items or [] if isinstance(item, dict)) if r]


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


def find_songs(queries: list[str]) -> list[Track]:
    """The first song found for each query, in their order; misses and repeats are skipped."""

    def first(query: str) -> Result | None:
        try:
            return next(
                filter(None, map(_song, _client().search(query, filter="songs", limit=1))), None
            )
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        found = list(pool.map(first, queries))
    tracks: dict[str, Track] = {}
    for result in found:
        if result and result.id not in tracks:
            tracks[result.id] = Track(result.id, result.title)
    return list(tracks.values())


def tracks(results: list[Result]) -> list[Track]:
    return [Track(r.id, r.title) for r in results if r.kind == SONGS]


def album(browse_id: str) -> tuple[str, list[Track]]:
    """An album's title and its playable tracks, in order."""
    page = album_page(browse_id)
    return page.title, tracks(page.sections[0].results)


def album_page(browse_id: str) -> Page:
    """An album: its tracks, then its artists."""
    data = _client().get_album(browse_id)
    artists = [a for a in data.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    artist = _names(artists)
    count = data.get("trackCount")
    detail = [data.get("type") or "Album", data.get("year"), count and f"{count} songs"]
    detail.append(data.get("duration"))
    page = Page(
        display_title(artist, data.get("title") or ""),
        " · ".join(str(x) for x in detail if x),
        [Section("", _parsed(data.get("tracks"), _song, artist), numbered=True)],
        plays_as=ALBUM_SOURCE,
        artist=artist,
    )
    links = [Result(ARTISTS, a["id"], a["name"]) for a in artists if a.get("id")]
    if links:
        page.sections.append(Section("Artist" if len(links) == 1 else "Artists", links))
    return page


def artist(browse_id: str) -> Page:
    """An artist: their radio, top songs, albums, singles and similar artists."""
    data = _client().get_artist(browse_id)
    name = data.get("name") or ""
    listeners, subscribers = data.get("monthlyListeners"), data.get("subscribers")
    detail = [listeners and f"{listeners} monthly audience"]
    detail.append(subscribers and f"{subscribers} subscribers")
    page = Page(name, " · ".join(x for x in detail if x))
    if data.get("radioId"):
        page.sections.append(Section("", [Result(RADIO, data["radioId"], f"{name} radio")]))

    songs = data.get("songs") or {}
    results = _parsed(songs.get("results"), _song, name)
    if results and songs.get("browseId"):
        results.append(Result(MORE_SONGS, songs["browseId"], "all songs"))
    page.sections.append(Section("Top songs", results))
    for key, title in (("albums", "Albums"), ("singles", "Singles")):
        shelf = data.get(key) or {}
        results = _parsed(shelf.get("results"), _album, name)
        if results and shelf.get("browseId") and shelf.get("params"):
            more = Result(MORE_ALBUMS, shelf["browseId"], f"all {key}", params=shelf["params"])
            results.append(more)
        page.sections.append(Section(title, results))
    related = (data.get("related") or {}).get("results")
    page.sections.append(Section("Fans might also like", _parsed(related, _artist)))
    page.sections = [s for s in page.sections if s.results]
    return page


def artist_songs(browse_id: str, name: str) -> Page:
    """All of an artist's songs (MORE_SONGS): a playlist."""
    data = _client().get_playlist(browse_id, limit=200)
    songs = _parsed(data.get("tracks"), _song, name)
    return Page(f"{name}: songs", f"{len(songs)} songs", [Section("", songs)], plays_as="songs")


def artist_albums(channel_id: str, params: str, name: str, title: str) -> Page:
    """All of an artist's albums or singles (MORE_ALBUMS)."""
    albums = _parsed(_client().get_artist_albums(channel_id, params, limit=None), _album, name)
    return Page(f"{name}: {title}", f"{len(albums)} releases", [Section("", albums)])


def artist_radio(playlist_id: str) -> list[Track]:
    """The tracks of an artist's radio, as YouTube Music starts it."""
    data = _client().get_watch_playlist(playlistId=playlist_id, limit=50)
    return tracks(_parsed(data.get("tracks"), _song))


def lyrics(video_id: str) -> Lyrics | None:
    """A song's lyrics, with line timings when YouTube Music has them; None if it has none."""
    client = _client()
    browse_id = (client.get_watch_playlist(video_id, limit=1) or {}).get("lyrics")
    if not browse_id:
        return None
    data = client.get_lyrics(browse_id, timestamps=True)
    if not data or not data.get("lyrics"):
        return None
    source = data.get("source") or ""
    if data.get("hasTimestamps"):
        lines = [(line.start_time / 1000, line.text) for line in data["lyrics"]]
        return Lyrics(lines, True, source)
    return Lyrics([(None, text) for text in str(data["lyrics"]).splitlines()], False, source)
