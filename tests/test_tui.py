"""The terminal UI, driven headless through Textual's pilot."""

import asyncio
import os
import shutil
import wave

import pytest
from fakes import FakeSource, tid
from test_music import FakeYTMusic
from textual.widgets import Static, TabbedContent

from musictty import control, music, paths, player
from musictty.models import Track
from musictty.music import ALBUMS, ARTISTS, SONGS, Result
from musictty.radio import LIST_PROPERTY, UPNEXT_PROPERTY, LaunchSpec, Radio
from musictty.store import Store
from musictty.tui import MusicApp, TrackList
from musictty.youtube import Stream

S, A, B, C = (tid(n) for n in range(4))
SIZE = (100, 30)


def lines(lst: TrackList) -> list[str]:
    return [str(lst.get_option_at_index(i).prompt) for i in range(lst.option_count)]


def now(app: MusicApp) -> str:
    return str(app.query_one("#now", Static).content)


async def until(pilot, check, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await pilot.pause(0.05)
    raise AssertionError("timed out")


def test_with_the_radio_off(monkeypatch):
    store = Store()
    for t in (Track(S, "s"), Track(A, "a")):
        store.add_seed(t)
        store.add_play(t)
    for t in (Track(S, "s"), Track(A, "a"), Track(B, "b")):
        store.like(t)
    started = []

    async def fake_start(seed=None, **kwargs):
        started.append((seed, kwargs))

    monkeypatch.setattr(control, "start", fake_start)

    async def scenario():
        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            # nothing plays: it opens on the recent radios, like v0's bare `music`
            assert app.query_one(TabbedContent).active == "recent"
            assert "radio is off" in now(app)
            assert lines(app.track_list("recent")) == ["♥ a", "♥ s"]
            assert lines(app.track_list("liked")) == ["b", "a", "s"]  # newest first

            await pilot.press("down", "enter")  # restart the second recent radio
            await until(pilot, lambda: len(started) == 1)
            # a started radio brings you to its tab
            await until(pilot, lambda: app.query_one(TabbedContent).active == "radio")

            await pilot.press("4", "right")  # liked, from b down, in a loop
            await until(pilot, lambda: len(started) == 2)
            await pilot.press("4", "down", "left")  # from a down, a on repeat
            await until(pilot, lambda: len(started) == 3)

            await pilot.press("4", "delete")  # the cursor is still on a
            assert Store().liked() == [Track(S, "s"), Track(B, "b")]
            assert lines(app.track_list("liked")) == ["b", "s"]

            await pilot.press("3", "e")  # queueing with the radio off just plays the track
            await until(pilot, lambda: len(started) == 4)
            await pilot.press("7")
            assert lines(app.track_list("upnext")) == ["radio is off"]

            await pilot.press("q")
        return app

    app = asyncio.run(scenario())
    assert app.return_code == 0
    queue_b = [Track(B, "b"), Track(A, "a"), Track(S, "s")]
    queue_a = [Track(A, "a"), Track(S, "s"), Track(B, "b")]
    assert started == [
        (S, {}),
        (B, {"queue": queue_b, "repeat_one": False}),
        (A, {"queue": queue_a, "repeat_one": True}),
        (A, {}),
    ]


def test_search(monkeypatch):
    songs = [Result(SONGS, S, "Daft Punk — Digital Love", "4:58")]
    albums = [Result(ALBUMS, "MPREb_1", "Daft Punk — Discovery", "Album · 2001")]
    artists = [Result(ARTISTS, "UCdp", "Daft Punk")]
    album = [Track(A, "Daft Punk — One More Time"), Track(B, "Daft Punk — Aerodynamic")]
    top = [Result(SONGS, B, "Daft Punk — Around the World")]
    monkeypatch.setattr(
        music,
        "search",
        lambda q: music.Found(songs, albums, artists) if q == "daft punk" else music.Found(),
    )
    monkeypatch.setattr(music, "album", lambda browse_id: ("Daft Punk — Discovery", album))
    page = music.Page("Daft Punk", sections=[music.Section("Top songs", top)])
    monkeypatch.setattr(music, "artist", lambda browse_id: page)
    started = []

    async def fake_start(seed=None, **kwargs):
        started.append((seed, kwargs))

    monkeypatch.setattr(control, "start", fake_start)
    results = [
        "Songs",
        "Daft Punk — Digital Love  4:58",
        "",
        "Albums",
        "Daft Punk — Discovery  Album · 2001",
        "",
        "Artists",
        "Daft Punk",
    ]

    async def scenario():
        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            search = app.track_list("search")
            await pilot.press("slash", *"daft punk", "enter")
            await until(pilot, lambda: lines(search) == results)
            assert app.query_one(TabbedContent).active == "search"
            assert search.highlighted == 1  # headings are skipped

            await pilot.press("enter")  # a song: a radio from it
            await until(pilot, lambda: len(started) == 1)

            await pilot.press("5", "down", "enter")  # an album: in order, then a radio
            await until(pilot, lambda: len(started) == 2)

            await pilot.press("5", "down", "enter")  # an artist: their page
            await until(pilot, lambda: lines(search)[-1] == "Daft Punk — Around the World")
            await pilot.press("left")
            await until(pilot, lambda: lines(search) == results)
            await pilot.press("down", "down", "enter", "escape")  # esc goes back too
            await until(pilot, lambda: lines(search) == results)

            await pilot.press("slash", *"zzz", "enter")
            await until(pilot, lambda: lines(search) == ["nothing found: «zzz»"])
            await pilot.press("q")

    asyncio.run(scenario())
    assert started == [(S, {}), (A, {"queue": album, "then_radio": True, "source": "album"})]


def test_ai_radio(monkeypatch):
    moods = []

    async def fake_ai_radio(mood):
        moods.append(mood)
        if mood == "broken":
            raise control.Failure("no ai key")
        return [Track(S, "s"), Track(A, "a")]

    monkeypatch.setattr(control, "ai_radio", fake_ai_radio)

    async def scenario():
        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            field = app.query_one("#search")
            await pilot.press("a")
            assert "mood" in field.placeholder
            await pilot.press(*"calm evening", "enter")
            await until(pilot, lambda: moods == ["calm evening"])
            await until(pilot, lambda: app.query_one(TabbedContent).active == "radio")
            assert "search" in field.placeholder  # back to search once the input is left
            await pilot.press("a", *"broken", "enter")
            await until(pilot, lambda: len(moods) == 2)
            await pilot.press("slash", "escape", "q")

    asyncio.run(scenario())


def test_lyrics(monkeypatch):
    timed = music.Lyrics([(1.0, "first"), (4.0, ""), (6.0, "third")], True, "Source: LF")
    fetched = []

    def fake_lyrics(video_id):
        fetched.append(video_id)
        return timed if video_id == S else None

    monkeypatch.setattr(music, "lyrics", fake_lyrics)

    async def scenario():
        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            view = app.track_list("lyrics")
            await pilot.press("6")
            assert lines(view) == ["radio is off"]

            def play(video_id, title):
                app.props[LIST_PROPERTY] = [
                    {"entry": 1, "id": video_id, "title": title, "current": True}
                ]
                app.player_changed(LIST_PROPERTY)

            play(S, "Artist — Song")
            await until(pilot, lambda: len(lines(view)) > 1)
            assert lines(view) == ["Artist — Song", "", "first", "♪", "third", "", "Source: LF"]
            app.position = 4.5  # the line being sung is highlighted
            app.sync_lyrics()
            assert view.highlighted == 3

            play(A, "Other")
            await until(pilot, lambda: lines(view)[-1] == "no lyrics for this track")
            play(S, "Artist — Song")  # already fetched: no new request
            await until(pilot, lambda: lines(view)[2:3] == ["first"])
            assert fetched == [S, A]
            await pilot.press("q")

    asyncio.run(scenario())


@pytest.mark.skipif(
    not shutil.which("mpv") and not os.environ.get("MUSICTTY_REQUIRE_MPV"), reason="needs mpv"
)
def test_with_a_real_player(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICTTY_MPV_ARGS", "--ao=null")
    files = {}
    for video_id in (S, A, B):
        files[video_id] = str(tmp_path / f"{video_id}.wav")
        with wave.open(files[video_id], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000 * 60)

    class LocalSource(FakeSource):
        def stream(self, video_id):
            return Stream(video_id, f"player {video_id}", files[video_id])

    source = LocalSource({S: [S, A, B]})
    spec = LaunchSpec(seed=S, stream=source.stream(S))
    store = Store()

    async def scenario():
        address = paths.ipc_address()
        proc = player.launch_mpv(spec, address)
        mpv = await player.wait_connect(address, lambda: proc.poll() is None)
        radio = Radio(mpv, spec, store=store, source=source)
        task = asyncio.create_task(radio.run())

        def playing():
            return (app.current() or {}).get("id")

        def played(*ids):
            return [t.id for t in store.recent_plays()] == list(ids)

        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            await until(pilot, lambda: played(S) and playing() == S)
            assert app.query_one(TabbedContent).active == "radio"
            assert now(app).startswith(f"▶ {source.title(S)}\nradio mix · ")

            await pilot.press("space")
            await until(pilot, lambda: now(app).startswith("‖ "))
            await pilot.press("space")
            await until(pilot, lambda: now(app).startswith("▶ "))

            await pilot.press("n")
            await until(pilot, lambda: played(A, S) and playing() == A)
            assert lines(app.track_list("radio")) == [
                f"▶ {source.title(A)}",
                f"  {source.title(S)}",
            ]

            await pilot.press("l", "r", "plus")
            await until(pilot, lambda: app.repeating() and app.props.get("volume") == 75)
            assert store.liked() == [Track(A, source.title(A))]
            assert " · ↻ ♥ · " in now(app)
            assert lines(app.track_list("radio"))[0] == f"▶ ↻ ♥ {source.title(A)}"
            await pilot.press("r")
            await until(pilot, lambda: not app.repeating())

            await pilot.press("1", "down", "left")  # jump back to the seed
            await until(pilot, lambda: played(S, A, S) and playing() == S)

            await pilot.press("s")
            await asyncio.wait_for(task, 5)  # the radio ends with its player
            await until(pilot, lambda: "radio is off" in now(app))
            await pilot.press("q")
        assert proc.wait(timeout=5) == 0

    asyncio.run(scenario())
    assert store.settings().volume == 75


@pytest.mark.skipif(
    not shutil.which("mpv") and not os.environ.get("MUSICTTY_REQUIRE_MPV"), reason="needs mpv"
)
def test_the_queue_with_a_real_player(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSICTTY_MPV_ARGS", "--ao=null")
    files = {}
    for video_id in (S, A, B, C):
        files[video_id] = str(tmp_path / f"{video_id}.wav")
        with wave.open(files[video_id], "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000 * 60)

    class LocalSource(FakeSource):
        def stream(self, video_id):
            return Stream(video_id, f"player {video_id}", files[video_id])

    source = LocalSource({S: [S, A, B]})
    spec = LaunchSpec(seed=S, stream=source.stream(S))
    store = Store()
    store.add_play(Track(C, "Earlier — C"))
    title = source.title

    async def scenario():
        address = paths.ipc_address()
        proc = player.launch_mpv(spec, address)
        mpv = await player.wait_connect(address, lambda: proc.poll() is None)
        radio = Radio(mpv, spec, store=store, source=source)
        task = asyncio.create_task(radio.run())

        def playing():
            return (app.current() or {}).get("id")

        upnext = None

        def shows(*expected):
            return lines(upnext) == list(expected)

        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            upnext = app.track_list("upnext")
            await until(pilot, lambda: playing() == S and len(app.props[UPNEXT_PROPERTY]) == 2)
            await pilot.press("7")
            await until(pilot, lambda: shows("from the radio mix", title(A), title(B)))
            assert upnext.highlighted == 1  # the heading is skipped

            await pilot.press("3", "down", "e")  # C from the history, after the current track
            await pilot.press("7")
            radio_mix = ("", "from the radio mix")
            await until(pilot, lambda: shows("Earlier — C", *radio_mix, title(A), title(B)))
            await pilot.press("3", "up", "E")  # S from the history, to play next
            await pilot.press("7")
            await until(
                pilot, lambda: shows(title(S), "Earlier — C", *radio_mix, title(A), title(B))
            )

            await pilot.press("home", "shift+down")  # S goes down one
            await until(
                pilot, lambda: shows("Earlier — C", title(S), *radio_mix, title(A), title(B))
            )
            assert upnext.highlighted == 1  # and the cursor with it
            await pilot.press("delete")
            await until(pilot, lambda: shows("Earlier — C", *radio_mix, title(A), title(B)))
            await pilot.press("down", "down", "E")  # B to the top
            await until(pilot, lambda: shows(title(B), "Earlier — C", *radio_mix, title(A)))

            await pilot.press("R")
            await until(pilot, lambda: app.repeating_all() and " · repeat all · " in now(app))
            # one loop: no heading for the radio's tracks
            await until(pilot, lambda: shows(title(B), "Earlier — C", title(A)))
            await pilot.press("R")
            await until(pilot, lambda: not app.repeating_all())

            await pilot.press("full_stop")
            await until(pilot, lambda: (app.position or 0) >= 10)
            await pilot.press("comma")
            await until(pilot, lambda: (app.position or 0) < 10)

            await pilot.press("down", "down", "enter")  # A now; the ones before it wait
            await until(pilot, lambda: playing() == A)
            await until(pilot, lambda: shows(title(B), "Earlier — C"))

            await pilot.press("s")
            await asyncio.wait_for(task, 5)
            await pilot.press("q")
        assert proc.wait(timeout=5) == 0

    asyncio.run(scenario())


def test_artist_and_album_pages(monkeypatch):
    monkeypatch.setattr(music, "_client", FakeYTMusic)
    started = []

    async def fake_start(seed=None, **kwargs):
        started.append((seed, kwargs))

    monkeypatch.setattr(control, "start", fake_start)
    album_page = [
        "Daft Punk — Discovery  (← back)",
        "Album · 2001 · 14 songs · 1 hour, 1 minute",
        "",
        " 1. One More Time  5:20",
        " 2. Daft Punk, Todd Edwards — Face to Face  4:00",
        "",
        "Artist",
        "Daft Punk",
    ]
    artist_page = [
        "Daft Punk  (← back)",
        "29.1M monthly audience · 8.2M subscribers",
        "",
        "▶ Daft Punk radio",
        "",
        "Top songs",
        "Daft Punk — Around the World  7:09",
        "all songs  →",
        "",
        "Albums",
        "Daft Punk — Discovery  Album · 2001",
        "all albums  →",
        "",
        "Singles",
        "Daft Punk — Get Lucky  Single · 2013",
        "",
        "Fans might also like",
        "Justice  1.2M subscribers",
    ]

    async def scenario():
        app = MusicApp()
        async with app.run_test(size=SIZE) as pilot:
            search = app.track_list("search")
            await pilot.press("slash", *"daft punk", "enter")
            await until(pilot, lambda: "Albums" in lines(search))
            results = lines(search)
            at = results.index("Daft Punk — Discovery  Album · 2001")
            search.highlighted = at
            await pilot.press("right")  # → opens the album; enter would play it
            await until(pilot, lambda: lines(search) == album_page)
            assert search.highlighted == 3  # on its first track
            await pilot.press("down", "enter")  # from the second track on, then a radio
            await until(pilot, lambda: len(started) == 1)

            await pilot.press("5", "down", "enter")  # its artist
            await until(pilot, lambda: lines(search) == artist_page)
            await pilot.press("enter")  # the artist's radio
            await until(pilot, lambda: len(started) == 2)

            await pilot.press("5", "down", "down", "enter")  # all songs
            await until(
                pilot, lambda: lines(search)[:2] == ["Daft Punk: songs  (← back)", "2 songs"]
            )
            await pilot.press("down", "enter")  # the second song, then the rest after it
            await until(pilot, lambda: len(started) == 3)

            await pilot.press("5", "left")
            await until(pilot, lambda: lines(search) == artist_page)
            search.highlighted = artist_page.index("all albums  →")
            await pilot.press("right")
            await until(pilot, lambda: lines(search)[0] == "Daft Punk: albums  (← back)")
            assert lines(search)[3:] == [
                "Daft Punk — Discovery  Album · 2001",
                "Daft Punk — Homework  Album · 1997",
            ]
            await pilot.press("escape", "escape")  # esc goes back too
            await until(pilot, lambda: lines(search) == album_page)
            await pilot.press("left")
            await until(pilot, lambda: lines(search) == results)
            await pilot.press("q")

    asyncio.run(scenario())
    one_more, face = (
        Track(S, "Daft Punk — One More Time"),
        Track(B, "Daft Punk, Todd Edwards — Face to Face"),
    )
    assert started[0] == (B, {"queue": [face], "then_radio": True, "source": "album"})
    radio = [Track(B, "Daft Punk — Around the World"), Track(C, "Justice — D.A.N.C.E.")]
    assert started[1] == (B, {"queue": radio, "then_radio": True, "source": "artist radio"})
    assert started[2] == (S, {"queue": [one_more], "then_radio": True, "source": "songs"})
