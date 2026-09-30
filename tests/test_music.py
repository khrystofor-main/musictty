import pytest
from fakes import tid

from musictty import music
from musictty.models import Track
from musictty.music import (
    ALBUMS,
    ARTISTS,
    MOODS,
    MORE_ALBUMS,
    MORE_SONGS,
    PLAYLISTS,
    RADIO,
    SONGS,
    Result,
)

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
        "playlists": [
            {
                "category": "Community playlists",
                "resultType": "playlist",
                "browseId": "VLPLfrench",
                "title": "French touch",
                "author": "Someone",
                "itemCount": 174,
            },
            # itemCount can be None for community playlists
            {"resultType": "playlist", "browseId": "VLPLx", "title": "No count", "itemCount": None},
            {"resultType": "playlist", "title": "no browse id"},
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
        if playlist_id == "VLPLfrench":
            return {
                "id": "PLfrench",
                "privacy": "PUBLIC",
                "title": "French touch",
                "author": {"name": "Someone", "id": "UCsomeone"},
                "year": "2020",
                "duration": "6+ hours",
                "trackCount": 174,
                "tracks": [
                    song(S, "One More Time", "Daft Punk", duration="5:20"),
                    song(A, "Gone", isAvailable=False),
                    song(C, "Music Sounds Better with You", "Stardust", duration="4:21"),
                ],
            }
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

    def get_search_suggestions(self, query, detailed_runs=False):
        assert not detailed_runs
        return {
            "fade": [
                "faded",
                "faded alan walker lyrics",
                "faded alan walker",
                "faded remix",
                "faded song",
                "faded lyrics",
                "faded instrumental",
                "faded 1 hour",
                "faded slowed",
            ],
            "zzz": [],
        }[query]

    def get_home(self, limit=3):
        assert limit == 6
        return [
            {
                "title": "Quick picks",
                "contents": [
                    {"title": "Gravity", "videoId": A, "thumbnails": [],
                     "artists": [{"name": "yetep", "id": "UCyetep"}],
                     "album": {"name": "Gravity", "id": "MPREb_gravity"}},
                    # a watch playlist: a radio
                    {"title": "Chill mix", "playlistId": "RDCLAKmix", "thumbnails": []},
                ],
            },
            {
                "title": "Your favorites",
                "contents": [
                    {"title": "Chill Satellite", "browseId": "UCsat", "subscribers": "374",
                     "thumbnails": []},
                    {"title": "Dragon", "year": "2019", "browseId": "MPREb_dragon",
                     "thumbnails": []},
                    {"title": "r/EDM top", "playlistId": "PLedm", "thumbnails": [],
                     "description": "redditEDM • 161 songs", "count": "161",
                     "author": [{"name": "redditEDM", "id": "UCedm"}]},
                    {"title": "A podcast", "browseId": "MPSPPLpod"},  # not music: left out
                ],
            },
            {"title": "Empty", "contents": []},
        ]  # fmt: skip

    def get_explore(self):
        return {
            "new_releases": [
                {
                    "title": "Hangang",
                    "type": "Album",
                    "browseId": "MPREb_new",
                    "isExplicit": False,
                    "artists": [{"id": "UCdept", "name": "Dept"}],
                    "audioPlaylistId": "OLAK5uy_new",
                    "thumbnails": [],
                },
            ],  # fmt: skip
            # top_songs only comes with a premium account
            "moods_and_genres": [{"title": "Chill", "params": "chill-params"}],
            "trending": {
                "playlist": "VLOLAKtrending",
                "items": [
                    {
                        "title": "Permission to Dance",
                        "videoId": B,
                        "playlistId": "OLAKtrending",
                        "videoType": "MUSIC_VIDEO_TYPE_OMV",
                        "views": "108M",
                        "thumbnails": [],
                        "artists": [{"name": "BTS", "id": "UCbts"}],
                        "isExplicit": False,
                    },
                ],  # fmt: skip
            },
            "new_videos": [],
        }

    def get_charts(self, country="ZZ"):
        assert country == "ZZ"
        return {
            "countries": {"selected": {"text": "Global"}, "options": ["DE", "ZZ"]},
            "videos": [
                {"title": "Top 100 Music Videos Global", "playlistId": "PLglobal", "thumbnails": []}
            ],
            "artists": [
                # without an account: unranked
                {
                    "title": "Bad Bunny",
                    "browseId": "UCbb",
                    "subscribers": "50M",
                    "thumbnails": [],
                    "rank": None,
                    "trend": None,
                },
            ],  # fmt: skip
        }

    def get_mood_categories(self):
        return {
            "Moods & moments": [{"params": "chill-params", "title": "Chill"}],
            "Genres": [{"params": "dance-params", "title": "Dance & Electronic"}, {"title": "?"}],
        }

    def get_mood_playlists(self, params):
        assert params == "chill-params"
        # get_library_playlists' format
        return [
            {"title": "Chill Hits", "playlistId": "RDCLAKchill", "thumbnails": [],
             "description": "Playlist • YouTube Music", "owned": False},
            {"title": "Lo-fi", "playlistId": "PLfrench", "thumbnails": [], "owned": False,
             "description": "Someone • 50 songs", "count": "50",
             "author": [{"name": "Someone", "id": "UCsomeone"}]},
        ]  # fmt: skip

    def get_watch_playlist(self, videoId=None, playlistId=None, limit=25, radio=False):
        if videoId:  # a track's radio: the track itself first, with its album and artists
            first = {"videoId": videoId, "title": "Lonely", "artists": []}
            if videoId == S:
                first = {
                    "videoId": S,
                    "title": "One More Time",
                    "length": "5:20",
                    "artists": [{"name": "Daft Punk", "id": "UCdp"}, {"name": "Romanthony"}],
                    "album": {"name": "Discovery", "id": "MPREb_1"},
                    "year": "2001",
                }
            return {"tracks": [first], "playlistId": "RDAMVM" + videoId, "lyrics": None}
        assert playlistId in ("RDEMdp", "RDCLAKmix")  # an artist's radio, a radio of Home
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
    assert found.playlists == [
        Result(PLAYLISTS, "VLPLfrench", "French touch", "Someone · 174 songs"),
        Result(PLAYLISTS, "VLPLx", "No count"),
    ]
    assert sorted(FakeYTMusic.calls) == [
        ("daft punk", "albums", 5),
        ("daft punk", "artists", 5),
        ("daft punk", "playlists", 5),
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
                Result(MORE_SONGS, "VLPLdp", "all songs", params="Daft Punk: songs"),
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
    songs = music.songs_page("VLPLdp", "Daft Punk: songs")
    assert songs.title == "Daft Punk: songs" and songs.plays_as
    assert [r.id for r in songs.sections[0].results] == [B, S]
    albums = music.artist_albums("UCdp", "albums-params", "Daft Punk", "albums")
    assert albums.title == "Daft Punk: albums"
    assert albums.sections[0].results == [
        Result(ALBUMS, "MPREb_1", "Daft Punk — Discovery", "Album · 2001"),
        Result(ALBUMS, "MPREb_2", "Daft Punk — Homework", "Album · 1997"),
    ]


def test_playlist():
    page = music.playlist_page("VLPLfrench")
    assert (page.title, page.detail) == ("French touch", "Someone · 174 songs · 6+ hours")
    assert page.plays_as == "playlist"
    assert music.playlist("VLPLfrench") == (
        "French touch",
        [
            Track(S, "Daft Punk — One More Time"),
            Track(C, "Stardust — Music Sounds Better with You"),
        ],
    )


def test_explore():
    page = music.explore()
    assert page.title == "Home"
    assert [(s.title, s.results) for s in page.sections] == [
        # Home's rows first, as they come
        (
            "Quick picks",
            [
                Result(SONGS, A, "yetep — Gravity"),
                Result(RADIO, "RDCLAKmix", "Chill mix", params="radio"),
            ],
        ),
        (
            "Your favorites",
            [
                Result(ARTISTS, "UCsat", "Chill Satellite", "374 subscribers"),
                Result(ALBUMS, "MPREb_dragon", "Dragon", "Album · 2019"),
                Result(PLAYLISTS, "VLPLedm", "r/EDM top", "redditEDM · 161 songs"),
            ],
        ),
        ("New releases", [Result(ALBUMS, "MPREb_new", "Dept — Hangang", "Album")]),
        (
            "Trending",
            [
                Result(SONGS, B, "BTS — Permission to Dance"),
                Result(MORE_SONGS, "VLOLAKtrending", "all trending", params="Trending"),
            ],
        ),
        ("Charts", [Result(PLAYLISTS, "VLPLglobal", "Top 100 Music Videos Global")]),
        ("Top artists", [Result(ARTISTS, "UCbb", "Bad Bunny", "50M subscribers")]),
        ("Moods & moments", [Result(MOODS, "chill-params", "Chill")]),
        ("Genres", [Result(MOODS, "dance-params", "Dance & Electronic")]),
    ]


def test_explore_keeps_what_loaded(monkeypatch):
    class Half(FakeYTMusic):
        def get_charts(self, country="ZZ"):
            raise ConnectionError

        def get_explore(self):
            raise ConnectionError

    monkeypatch.setattr(music, "_client", Half)
    titles = [s.title for s in music.explore().sections]
    assert titles == ["Quick picks", "Your favorites", "Moods & moments", "Genres"]


def test_mood_playlists():
    page = music.mood_playlists("chill-params", "Chill")
    assert (page.title, page.detail) == ("Chill", "2 playlists")
    assert page.sections[0].results == [
        Result(PLAYLISTS, "VLRDCLAKchill", "Chill Hits", "Playlist • YouTube Music"),
        Result(PLAYLISTS, "VLPLfrench", "Lo-fi", "Someone · 50 songs"),
    ]


def test_suggestions():
    assert music.suggestions("fade") == [
        "faded",
        "faded alan walker lyrics",
        "faded alan walker",
        "faded remix",
        "faded song",
        "faded lyrics",
        "faded instrumental",
        "faded 1 hour",
    ]
    assert music.suggestions("fade", limit=2) == ["faded", "faded alan walker lyrics"]
    assert music.suggestions("zzz") == []


def test_track_links():
    assert music.track_links(S) == [
        Result(ARTISTS, "UCdp", "Daft Punk"),  # an artist without an id has no page
        Result(ALBUMS, "MPREb_1", "Daft Punk, Romanthony — Discovery"),
    ]
    assert music.track_links(A) == []


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
