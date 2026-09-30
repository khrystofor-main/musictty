"""YouTube Music's catalogue through ytmusicapi: search, albums, artists' pages.

yt-dlp still plays everything and builds the mixes; its search only gives bare ids for
albums and artists, while ytmusicapi returns titles, artists and years in one request.
Every call blocks on the network: async code runs it in a thread. ytmusicapi is imported
lazily, like yt_dlp.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .models import Track
from .radio import ALBUM as ALBUM_SOURCE
from .radio import PLAYLIST as PLAYLIST_SOURCE
from .youtube import display_title, is_video_id

PLAYLIST_LIMIT = 200  # tracks read from a playlist
HOME_ROWS = 6  # rows of Home, before Explore's
RADIO_LABEL = "radio"  # the source a radio of Home plays as
# the country of the charts (ISO 3166-1 alpha-2); ZZ is the whole world
CHARTS_COUNTRY = os.environ.get("MUSICTTY_CHARTS", "ZZ").upper()

SONGS, ALBUMS, ARTISTS, PLAYLISTS = "song", "album", "artist", "playlist"
RADIO = "radio"  # an artist's radio, a mix of Home: id is its playlist id, params its label
MORE_SONGS = "more songs"  # a longer list of songs: id is its playlist's browse id
MORE_ALBUMS = "more albums"  # all albums or singles: id is the channel id, with params
MOODS = "mood"  # a mood or a genre of Explore: id is its params
MORE_RESULTS = "more results"  # more of one kind of search results: id is the query


@dataclass(frozen=True)
class Result:
    # SONGS, ALBUMS, ARTISTS, PLAYLISTS; RADIO, MORE_SONGS, MORE_ALBUMS on an artist's page
    kind: str
    id: str  # video id, album / playlist browse id or artist channel id
    title: str  # "Artist — Title" for songs and albums, the name for artists
    detail: str = ""  # duration of a song, "Album · 1995" of an album
    # what ytmusicapi needs besides the id (MORE_ALBUMS); the title of MORE_SONGS' page
    params: str = ""


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
    playlists: list[Result] = field(default_factory=list)


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


def _playlist(item: dict) -> Result | None:
    browse_id = item.get("browseId") or (item.get("playlistId") and "VL" + item["playlistId"])
    if not browse_id or not item.get("title"):
        return None
    # search: author and itemCount; Explore's moods: author as a list, count; charts: nothing
    author = item.get("author")
    author = _names(author) if isinstance(author, list) else author
    count = item.get("itemCount", item.get("count"))
    detail = " · ".join(str(x) for x in (author, count is not None and f"{count} songs") if x)
    detail = detail or item.get("description") or ""
    return Result(PLAYLISTS, browse_id, item["title"], detail)


def _parsed(items: Any, parse: Any, *args: Any) -> list[Result]:
    return [r for r in (parse(item, *args) for item in items or [] if isinstance(item, dict)) if r]


SEARCH_KINDS = {"songs": _song, "albums": _album, "artists": _artist, "playlists": _playlist}
SEARCH_LIMIT = 8  # results of each kind
MORE_LIMIT = 50  # results of one kind, for "all songs" and the like


def search(query: str, limit: int = SEARCH_LIMIT) -> Found:
    """Songs, albums, artists and playlists for a query: four requests at once."""
    kinds = tuple(SEARCH_KINDS.items())

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


def suggestions(query: str, limit: int = 8) -> list[str]:
    """What YouTube Music suggests while a search is typed."""
    found = _client().get_search_suggestions(query)
    return [text for text in found or [] if isinstance(text, str) and text.strip()][:limit]


def search_more(query: str, kind: str) -> Page:
    """More results of one kind (MORE_RESULTS): all the songs for a query, and so on."""
    found = _client().search(query, filter=kind, limit=MORE_LIMIT)
    results = _parsed(found, SEARCH_KINDS[kind])[:MORE_LIMIT]
    title = f"{kind.capitalize()} for «{query}»"
    return Page(title, f"{len(results)} found", [Section("", results)])


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
        results.append(Result(MORE_SONGS, songs["browseId"], "all songs", params=f"{name}: songs"))
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


def playlist_page(browse_id: str) -> Page:
    """A YouTube Music playlist: its tracks."""
    data = _client().get_playlist(browse_id, limit=PLAYLIST_LIMIT)
    songs = _parsed(data.get("tracks"), _song)
    author = data.get("author")
    author = author.get("name") if isinstance(author, dict) else author
    count = data.get("trackCount")
    detail = [author, count is not None and f"{count} songs", data.get("duration")]
    title = data.get("title") or "playlist"
    detail_line = " · ".join(str(x) for x in detail if x)
    return Page(title, detail_line, [Section("", songs, numbered=True)], plays_as=PLAYLIST_SOURCE)


def playlist(browse_id: str) -> tuple[str, list[Track]]:
    """A YouTube Music playlist's title and its playable tracks."""
    page = playlist_page(browse_id)
    return page.title, tracks(page.sections[0].results)


def songs_page(browse_id: str, title: str) -> Page:
    """A longer list of songs (MORE_SONGS): all of an artist's, all of the trending ones."""
    data = _client().get_playlist(browse_id, limit=PLAYLIST_LIMIT)
    songs = _parsed(data.get("tracks"), _song)
    return Page(title, f"{len(songs)} songs", [Section("", songs)], plays_as="songs")


def artist_albums(channel_id: str, params: str, name: str, title: str) -> Page:
    """All of an artist's albums or singles (MORE_ALBUMS)."""
    albums = _parsed(_client().get_artist_albums(channel_id, params, limit=None), _album, name)
    return Page(f"{name}: {title}", f"{len(albums)} releases", [Section("", albums)])


def _home_item(item: dict) -> Result | None:
    """A row of Home mixes songs, albums, artists, playlists and radios."""
    browse_id = item.get("browseId") or ""
    if item.get("videoId"):
        return _song(item)
    if browse_id.startswith("MPRE"):
        return _album(item)
    if browse_id.startswith("UC"):
        return _artist(item)
    if item.get("playlistId") and "owned" not in item and "author" not in item:
        # a watch playlist: a radio of YouTube Music's, played like an artist's
        return Result(RADIO, item["playlistId"], item.get("title") or "radio", params=RADIO_LABEL)
    return _playlist(item) if item.get("playlistId") else None


def explore() -> Page:
    """YouTube Music's Home and Explore without an account: Home's rows (quick picks and the
    like), new releases, trending songs, the charts, moods and genres. Four requests at once;
    a part that fails is left out."""

    def get(method: str, *args: Any) -> Any:
        try:
            return getattr(_client(), method)(*args) or {}
        except Exception:
            return {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        home_f = pool.submit(get, "get_home", HOME_ROWS)
        explore_f = pool.submit(get, "get_explore")
        charts_f = pool.submit(get, "get_charts", CHARTS_COUNTRY)
        moods_f = pool.submit(get, "get_mood_categories")
        home, data = home_f.result(), explore_f.result()
        charts, moods = charts_f.result(), moods_f.result()
    page = Page("Home")
    for row in home if isinstance(home, list) else []:
        if isinstance(row, dict) and row.get("title"):
            page.sections.append(Section(row["title"], _parsed(row.get("contents"), _home_item)))
    page.sections.append(Section("New releases", _parsed(data.get("new_releases"), _album)))
    for key, title in (("top_songs", "Top songs"), ("trending", "Trending")):
        shelf = data.get(key) or {}
        results = _parsed(shelf.get("items"), _song)
        if results and shelf.get("playlist"):
            more = Result(MORE_SONGS, shelf["playlist"], f"all {title.lower()}", params=title)
            results.append(more)
        page.sections.append(Section(title, results))
    chart_playlists = [*(charts.get("videos") or []), *(charts.get("genres") or [])]
    page.sections.append(Section("Charts", _parsed(chart_playlists, _playlist)))
    page.sections.append(Section("Top artists", _parsed(charts.get("artists"), _artist)))
    for title, categories in moods.items() if isinstance(moods, dict) else []:
        results = [
            Result(MOODS, c["params"], c["title"])
            for c in categories or []
            if isinstance(c, dict) and c.get("params") and c.get("title")
        ]
        page.sections.append(Section(title, results))
    page.sections = [s for s in page.sections if s.results]
    return page


def mood_playlists(params: str, title: str) -> Page:
    """The playlists of a mood or a genre."""
    playlists = _parsed(_client().get_mood_playlists(params), _playlist)
    return Page(title, f"{len(playlists)} playlists", [Section("", playlists)])


def track_links(video_id: str) -> list[Result]:
    """A track's artists and album, to go to their pages. The watch playlist of a track
    starts with the track itself, with the ids that a mix or a history entry doesn't have."""
    data = _client().get_watch_playlist(videoId=video_id, limit=1)
    tracks = [t for t in data.get("tracks") or [] if isinstance(t, dict)]
    track = next((t for t in tracks if t.get("videoId") == video_id), None)
    if track is None:
        return []
    artists = [a for a in track.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    links = [Result(ARTISTS, a["id"], a["name"]) for a in artists if a.get("id")]
    album = track.get("album")
    if isinstance(album, dict) and album.get("id") and album.get("name"):
        links.append(Result(ALBUMS, album["id"], display_title(_names(artists), album["name"])))
    return links


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
