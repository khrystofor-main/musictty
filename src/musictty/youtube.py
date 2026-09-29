"""YouTube Music through the yt-dlp library.

Every call here blocks on the network: async code runs it in a thread.
yt_dlp is imported lazily, so quick commands like `musictty next` don't pay for it.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .models import Track

log = logging.getLogger(__name__)

# light audio stream (opus/m4a ~128–160 kbit/s); mpv gets the same format for its ytdl hook
FORMAT = "bestaudio[acodec=opus]/bestaudio/best"

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_ID_IN_URL = re.compile(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})")


@dataclass(frozen=True)
class Stream:
    """A track resolved to a direct audio URL (lives ~6 hours)."""

    id: str
    title: str
    url: str
    user_agent: str = ""


def is_video_id(text: str) -> bool:
    return bool(_VIDEO_ID.fullmatch(text))


def video_id_from_url(url: str) -> str | None:
    m = _ID_IN_URL.search(url)
    return m.group(1) if m else None


def watch_url(video_id: str) -> str:
    return f"https://music.youtube.com/watch?v={video_id}"


def display_title(artist: str, title: str) -> str:
    return f"{artist} — {title}" if artist else title


class _Log:
    """Route yt-dlp's chatter into logging instead of the terminal."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp: %s", msg)


def _extract(url: str, **opts: Any) -> dict:
    import yt_dlp

    params = {"quiet": True, "no_warnings": True, "noprogress": True, "logger": _Log(), **opts}
    with yt_dlp.YoutubeDL(params) as ydl:
        return ydl.extract_info(url, download=False) or {}


def _artist(info: dict) -> str:
    return info.get("uploader") or info.get("channel") or ""


def _stream(info: dict) -> Stream | None:
    video_id, url = info.get("id") or "", info.get("url") or ""
    if not is_video_id(video_id) or not url.startswith(("http://", "https://")):
        return None
    title = display_title(_artist(info), info.get("title") or video_id)
    user_agent = (info.get("http_headers") or {}).get("User-Agent", "")
    return Stream(video_id, title, url, user_agent)


def resolve(video_id: str) -> Stream | None:
    """Title and direct stream of one track in a single request; None if it failed."""
    try:
        return _stream(_extract(watch_url(video_id), format=FORMAT, noplaylist=True))
    except Exception as e:
        log.info("could not resolve %s: %s", video_id, e)
        return None


def _first_id(url: str, limit: int) -> str | None:
    try:
        info = _extract(url, extract_flat="in_playlist", playlistend=limit)
    except Exception as e:
        log.info("search failed: %s", e)
        return None
    for entry in info.get("entries") or []:
        if entry and is_video_id(entry.get("id") or ""):
            return entry["id"]
    return None


def search(query: str) -> tuple[str, Stream | None] | None:
    """First song for the query: (id, stream), where the stream may be missing; None if nothing."""
    url = f"https://music.youtube.com/search?q={quote(query)}#songs"
    # usually one request gives the first song already resolved to a stream
    try:
        entries = _extract(url, format=FORMAT, playlist_items="1").get("entries") or []
        stream = _stream(entries[0]) if entries else None
        if stream:
            return stream.id, stream
    except Exception as e:
        log.info("search failed: %s", e)
    # the first result was a mix or a playlist: take the first real track id
    video_id = _first_id(url, 5) or _first_id(f"ytsearch1:{query}", 1)
    return (video_id, None) if video_id else None


def mix(seed: str, limit: int) -> list[Track]:
    """Tracks of the seed's radio mix (RDAMVM<id>), the seed itself first. Raises on failure."""
    url = f"{watch_url(seed)}&list=RDAMVM{seed}"
    info = _extract(url, extract_flat="in_playlist", playlistend=limit, noplaylist=False)
    tracks = []
    for entry in info.get("entries") or []:
        video_id = (entry or {}).get("id") or ""
        if is_video_id(video_id):
            title = display_title(_artist(entry), entry.get("title") or video_id)
            tracks.append(Track(video_id, title))
    return tracks


def check(stream: Stream) -> tuple[bool, str]:
    """YouTube now and then hands out a URL that answers 403 right away.

    Ask for the first kilobyte (a fraction of a second) so playback doesn't trip over it.
    A network hiccup counts as fine: the fallback at playback time will deal with it.
    """
    headers = {"Range": "bytes=0-1023"}
    if stream.user_agent:
        headers["User-Agent"] = stream.user_agent
    try:
        with urllib.request.urlopen(
            urllib.request.Request(stream.url, headers=headers), timeout=5
        ) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (OSError, ValueError):
        return True, "?"
    return code in (200, 206), str(code)
