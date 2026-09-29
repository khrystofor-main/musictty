import http.server
import threading

import pytest
from fakes import tid

from musictty import youtube
from musictty.models import Track
from musictty.youtube import Stream

S, A = tid(0), tid(1)


def info(video_id, **extra):
    return {"id": video_id, "title": f"Song {video_id}", "uploader": "Artist", **extra}


def full(video_id):
    return info(video_id, url=f"https://audio/{video_id}", http_headers={"User-Agent": "UA"})


@pytest.fixture
def requests(monkeypatch):
    """Replace yt-dlp: answers per URL, and a log of (url, options)."""
    answers, log = {}, []

    def extract(url, **opts):
        log.append((url, opts))
        answer = answers.get(url)
        if isinstance(answer, Exception):
            raise answer
        return answer or {}

    monkeypatch.setattr(youtube, "_extract", extract)
    return answers, log


def test_ids_and_links():
    assert youtube.video_id_from_url(f"https://music.youtube.com/watch?v={S}&si=x") == S
    assert youtube.video_id_from_url(f"https://youtu.be/{S}?t=3") == S
    assert youtube.video_id_from_url("https://www.youtube.com/playlist?list=PL1") is None
    assert youtube.is_video_id(S)
    assert not youtube.is_video_id("Кириллица11")


def test_resolve(requests):
    answers, log = requests
    answers[youtube.watch_url(S)] = full(S)
    assert youtube.resolve(S) == Stream(S, f"Artist — Song {S}", f"https://audio/{S}", "UA")
    assert log[0][1]["format"] == youtube.FORMAT


def test_resolve_failure_is_none(requests):
    answers, _ = requests
    answers[youtube.watch_url(S)] = RuntimeError("Video unavailable")
    assert youtube.resolve(S) is None


def test_search_resolves_the_first_song_in_one_request(requests):
    answers, log = requests
    url = "https://music.youtube.com/search?q=daft%20punk#songs"
    answers[url] = {"entries": [full(S)]}
    stream = Stream(S, f"Artist — Song {S}", f"https://audio/{S}", "UA")
    assert youtube.search("daft punk") == (S, stream)
    assert log == [(url, {"format": youtube.FORMAT, "playlist_items": "1"})]


def test_search_skips_a_mix_in_first_place(monkeypatch):
    url = "https://music.youtube.com/search?q=x#songs"
    mix = {"id": "RDCLAK5uy_mix", "title": "a mix"}

    def extract(u, **opts):
        assert u == url
        # resolved: the first result is a mix, no stream; flat: the mix, then a song
        return {"entries": [mix, {"id": S}]} if opts.get("extract_flat") else {"entries": [mix]}

    monkeypatch.setattr(youtube, "_extract", extract)
    assert youtube.search("x") == (S, None)


def test_search_nothing(requests):
    assert youtube.search("nothing at all") is None


def test_mix(requests):
    answers, log = requests
    url = f"{youtube.watch_url(S)}&list=RDAMVM{S}"
    answers[url] = {
        "entries": [info(S), {"id": A, "title": "No uploader", "channel": "Chan"}, {"id": "bad"}]
    }
    assert youtube.mix(S, 25) == [Track(S, f"Artist — Song {S}"), Track(A, "Chan — No uploader")]
    assert log[0][1]["playlistend"] == 25


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            Handler.seen = dict(self.headers)  # before answering: the client returns right after
            self.send_response(403 if "bad" in self.path else 206)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", Handler
    httpd.shutdown()


def test_check(server, monkeypatch):
    for proxy in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(proxy, raising=False)
    base, handler = server
    assert youtube.check(Stream(S, "t", f"{base}/good", "UA/1")) == (True, "206")
    assert handler.seen["Range"] == "bytes=0-1023"
    assert handler.seen["User-Agent"] == "UA/1"
    assert youtube.check(Stream(S, "t", f"{base}/bad")) == (False, "403")
    # a network hiccup is not a reason to drop the stream
    assert youtube.check(Stream(S, "t", "http://127.0.0.1:1/x")) == (True, "?")
