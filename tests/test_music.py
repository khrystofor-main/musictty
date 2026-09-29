import pytest
from fakes import tid

from musictty import music
from musictty.models import Track
from musictty.music import ALBUMS, ARTISTS, SONGS, Result

S, A, B = (tid(n) for n in range(3))


def song(video_id, title, *artists, **extra):
    return {
        "resultType": "song",
        "videoId": video_id,
        "title": title,
        "artists": [{"name": a, "id": "UC" + a} for a in artists],
        **extra,
    }


class FakeYTMusic:
    """ytmusicapi's YTMusic, shaped like its documented results."""

    results = {
        "songs": [
            song(S, "Digital Love", "Daft Punk", duration="4:58"),
            song(A, "Grey", "Nobody", isAvailable=False),
            song("bad", "No id", "Nobody"),
            song(B, "Solo", duration="3:00"),
        ],
        "albums": [
            {
                "resultType": "album",
                "browseId": "MPREb_1",
                "title": "Discovery",
                "type": "Album",
                "year": "2001",
                "artists": [{"name": "Daft Punk", "id": "UC1"}],
            },
            {"resultType": "album", "browseId": "MPREb_2", "title": "Single", "type": "Single"},
            {"resultType": "album", "title": "no browse id"},
        ],
        "artists": [
            {"resultType": "artist", "artist": "Daft Punk", "browseId": "UCdp"},
            {"resultType": "artist", "artist": "Nameless"},
        ],
    }
    calls: list = []

    def search(self, query, filter=None, limit=20):
        self.calls.append((query, filter, limit))
        return self.results[filter]

    def get_album(self, browse_id):
        return {
            "title": "Discovery",
            "artists": [{"name": "Daft Punk"}],
            "tracks": [song(S, "One More Time", "Daft Punk"), song(A, "Gone", isAvailable=False)],
        }

    def get_artist(self, browse_id):
        return {
            "name": "Daft Punk",
            "songs": {"results": [song(B, "Around the World", "Daft Punk")]},
        }


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeYTMusic.calls = []
    monkeypatch.setattr(music, "_client", FakeYTMusic)


def test_search():
    found = music.search("daft punk", limit=5)
    assert found.songs == [
        Result(SONGS, S, "Daft Punk — Digital Love", "4:58"),
        Result(SONGS, B, "Solo", "3:00"),  # unavailable songs and broken ids are left out
    ]
    assert found.albums == [
        Result(ALBUMS, "MPREb_1", "Daft Punk — Discovery", "Album · 2001"),
        Result(ALBUMS, "MPREb_2", "Single", "Single"),
    ]
    assert found.artists == [Result(ARTISTS, "UCdp", "Daft Punk")]
    assert sorted(FakeYTMusic.calls) == [
        ("daft punk", "albums", 5),
        ("daft punk", "artists", 5),
        ("daft punk", "songs", 5),
    ]


def test_album_keeps_only_playable_tracks():
    assert music.album("MPREb_1") == (
        "Daft Punk — Discovery",
        [Track(S, "Daft Punk — One More Time")],
    )


def test_artist_songs():
    assert music.artist_songs("UCdp") == (
        "Daft Punk",
        [Result(SONGS, B, "Daft Punk — Around the World")],
    )
