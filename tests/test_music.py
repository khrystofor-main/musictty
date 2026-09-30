import pytest
from fakes import tid

from musictty import music
from musictty.models import Track
from musictty.music import ALBUMS, ARTISTS, MORE_ALBUMS, MORE_SONGS, RADIO, SONGS, Result

S, A, B, C = (tid(n) for n in range(4))


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
            "type": "Album",
            "year": "2001",
            "trackCount": 14,
            "duration": "1 hour, 1 minute",
            "artists": [{"name": "Daft Punk", "id": "UCdp"}],
            "tracks": [
                song(S, "One More Time", "Daft Punk", duration="5:20", trackNumber=1),
                song(A, "Gone", isAvailable=False),
                song(B, "Face to Face", "Daft Punk", "Todd Edwards", duration="4:00"),
            ],
        }

    def get_artist(self, browse_id):
        return {
            "description": "Daft Punk were...",
            "views": "1,000 views",
            "name": "Daft Punk",
            "channelId": "UCother",
            "shuffleId": "RDAOdp",
            "radioId": "RDEMdp",
            "subscribers": "8.2M",
            "monthlyListeners": "29.1M",
            "subscribed": False,
            "thumbnails": [],
            "songs": {
                "browseId": "VLPLdp",
                # the artist's top songs: artists as a list, like playlist items
                "results": [song(B, "Around the World", "Daft Punk", duration="7:09")],
            },
            "albums": {
                "results": [
                    {"title": "Discovery", "thumbnails": [], "year": "2001", "browseId": "MPREb_1"},
                    {"title": "no browse id", "year": "1997"},
                ],
                "browseId": "UCdp",
                "params": "albums-params",
            },
            "singles": {
                "results": [
                    {"title": "Get Lucky", "type": "Single", "year": "2013", "browseId": "MPREb_3"}
                ],
                "browseId": "UCdp",
            },
            "videos": {"results": [], "browseId": "VLPLvideos"},
            "related": {
                "results": [
                    {"browseId": "UCjustice", "subscribers": "1.2M", "title": "Justice"},
                    {"title": "no browse id"},
                ]
            },
        }

    def get_playlist(self, playlist_id, limit=100, related=False, suggestions_limit=0):
        assert playlist_id == "VLPLdp"
        return {
            "id": "PLdp",
            "title": "Daft Punk songs",
            "trackCount": 2,
            "tracks": [
                song(B, "Around the World", "Daft Punk", duration="7:09"),
                song(S, "One More Time", "Daft Punk", duration="5:20"),
            ],
        }

    def get_artist_albums(self, channel_id, params, limit=100, order=None):
        assert (channel_id, params) == ("UCdp", "albums-params")
        # get_library_albums' format without the artists
        return [
            {"browseId": "MPREb_1", "playlistId": "OLAK1", "title": "Discovery", "type": "Album",
             "year": "2001", "thumbnails": []},
            {"browseId": "MPREb_2", "playlistId": "OLAK2", "title": "Homework", "type": "Album",
             "year": "1997", "thumbnails": []},
        ]  # fmt: skip

    def get_watch_playlist(self, videoId=None, playlistId=None, limit=25, radio=False):
        assert playlistId == "RDEMdp"
        tracks = [
            {"videoId": B, "title": "Around the World", "length": "7:09",
             "artists": [{"name": "Daft Punk", "id": "UCdp"}]},
            {"videoId": C, "title": "D.A.N.C.E.", "length": "4:02",
             "artists": [{"name": "Justice", "id": "UCjustice"}]},
        ]  # fmt: skip
        return {"tracks": tracks, "playlistId": playlistId, "lyrics": None, "related": None}


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
        [Track(S, "Daft Punk — One More Time"), Track(B, "Daft Punk, Todd Edwards — Face to Face")],
    )


def test_album_page():
    page = music.album_page("MPREb_1")
    assert page.title == "Daft Punk — Discovery"
    assert page.detail == "Album · 2001 · 14 songs · 1 hour, 1 minute"
    assert page.plays_as == "album" and page.artist == "Daft Punk"
    tracks, artists = page.sections
    assert tracks.numbered
    assert tracks.results == [
        Result(SONGS, S, "Daft Punk — One More Time", "5:20"),
        Result(SONGS, B, "Daft Punk, Todd Edwards — Face to Face", "4:00"),
    ]
    assert artists == music.Section("Artist", [Result(ARTISTS, "UCdp", "Daft Punk")])


def test_artist_page():
    page = music.artist("UCdp")
    assert page.title == "Daft Punk"
    assert page.detail == "29.1M monthly audience · 8.2M subscribers"
    assert not page.plays_as
    assert [(s.title, s.results) for s in page.sections] == [
        ("", [Result(RADIO, "RDEMdp", "Daft Punk radio")]),
        (
            "Top songs",
            [
                Result(SONGS, B, "Daft Punk — Around the World", "7:09"),
                Result(MORE_SONGS, "VLPLdp", "all songs"),
            ],
        ),
        (
            "Albums",
            [
                # albums on an artist's page come without the artist: it's filled in
                Result(ALBUMS, "MPREb_1", "Daft Punk — Discovery", "Album · 2001"),
                Result(MORE_ALBUMS, "UCdp", "all albums", params="albums-params"),
            ],
        ),
        # no params: no "all singles"
        ("Singles", [Result(ALBUMS, "MPREb_3", "Daft Punk — Get Lucky", "Single · 2013")]),
        ("Fans might also like", [Result(ARTISTS, "UCjustice", "Justice", "1.2M subscribers")]),
    ]


def test_artist_page_without_songs_or_radio(monkeypatch):
    class Bare(FakeYTMusic):
        def get_artist(self, browse_id):
            return {"name": "Nobody", "songs": {"browseId": None}, "subscribers": None}

    monkeypatch.setattr(music, "_client", Bare)
    assert music.artist("UCx") == music.Page("Nobody")


def test_all_of_an_artists_songs_and_albums():
    songs = music.artist_songs("VLPLdp", "Daft Punk")
    assert songs.title == "Daft Punk: songs" and songs.plays_as
    assert [r.id for r in songs.sections[0].results] == [B, S]
    albums = music.artist_albums("UCdp", "albums-params", "Daft Punk", "albums")
    assert albums.title == "Daft Punk: albums"
    assert albums.sections[0].results == [
        Result(ALBUMS, "MPREb_1", "Daft Punk — Discovery", "Album · 2001"),
        Result(ALBUMS, "MPREb_2", "Daft Punk — Homework", "Album · 1997"),
    ]


def test_artist_radio():
    assert music.artist_radio("RDEMdp") == [
        Track(B, "Daft Punk — Around the World"),
        Track(C, "Justice — D.A.N.C.E."),
    ]


def test_find_songs_keeps_order_and_skips_misses(monkeypatch):
    class Search(FakeYTMusic):
        def search(self, query, filter=None, limit=20):
            return {
                "one": [song(S, "One", "X")],
                "again": [song(S, "One", "X")],
                "two": [song(A, "Gone", isAvailable=False), song(B, "Two", "Y")],
            }.get(query, [])

    monkeypatch.setattr(music, "_client", Search)
    assert music.find_songs(["two", "missing", "one", "again"]) == [
        Track(B, "Y — Two"),
        Track(S, "X — One"),
    ]


class Line:
    """ytmusicapi's LyricLine."""

    def __init__(self, text, start_time):
        self.text, self.start_time, self.end_time, self.id = text, start_time, start_time + 1, 1


def lyrics_client(watch, lyrics):
    class Client(FakeYTMusic):
        def get_watch_playlist(self, video_id, limit=25):
            return watch

        def get_lyrics(self, browse_id, timestamps=False):
            assert browse_id == "MPLYt_1" and timestamps
            return lyrics

    return Client


def test_timed_lyrics(monkeypatch):
    data = {"lyrics": [Line("One", 1200), Line("Two", 3500)], "source": "LF", "hasTimestamps": True}
    monkeypatch.setattr(music, "_client", lyrics_client({"lyrics": "MPLYt_1"}, data))
    assert music.lyrics(S) == music.Lyrics([(1.2, "One"), (3.5, "Two")], True, "LF")


def test_plain_lyrics(monkeypatch):
    data = {"lyrics": "One\nTwo", "source": None, "hasTimestamps": False}
    monkeypatch.setattr(music, "_client", lyrics_client({"lyrics": "MPLYt_1"}, data))
    assert music.lyrics(S) == music.Lyrics([(None, "One"), (None, "Two")], False, "")


def test_no_lyrics(monkeypatch):
    monkeypatch.setattr(music, "_client", lyrics_client({"lyrics": None}, None))
    assert music.lyrics(S) is None
