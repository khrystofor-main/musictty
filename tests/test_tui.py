"""The terminal UI, driven headless through Textual's pilot."""

import asyncio
import os
import shutil
import wave

import pytest
from fakes import FakeSource, tid
from textual.widgets import Static, TabbedContent

from musictty import control, music, paths, player
from musictty.models import Track
from musictty.music import ALBUMS, ARTISTS, SONGS, Result
from musictty.radio import LaunchSpec, Radio
from musictty.store import Store
from musictty.tui import MusicApp, TrackList
from musictty.youtube import Stream

S, A, B = (tid(n) for n in range(3))
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
    monkeypatch.setattr(music, "artist_songs", lambda browse_id: ("Daft Punk", top))
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

            await pilot.press("5", "down", "enter")  # an artist: the top songs
            await until(pilot, lambda: lines(search)[1:] == ["Daft Punk — Around the World"])
            await pilot.press("left")
            await until(pilot, lambda: lines(search) == results)
            await pilot.press("down", "down", "enter", "escape")  # esc goes back too
            await until(pilot, lambda: lines(search) == results)

            await pilot.press("slash", *"zzz", "enter")
            await until(pilot, lambda: lines(search) == ["nothing found: «zzz»"])
            await pilot.press("q")

    asyncio.run(scenario())
    assert started == [(S, {}), (A, {"queue": album, "album": True})]


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
