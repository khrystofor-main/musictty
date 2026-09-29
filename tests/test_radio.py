import asyncio

import pytest
from fakes import FakeMpv, FakeSource, parse_options, tid

from musictty import radio as radio_module
from musictty.models import Track
from musictty.radio import (
    LIST_PROPERTY,
    SOURCE_PROPERTY,
    LaunchSpec,
    Radio,
    file_options,
    track_url,
)
from musictty.store import Store

S, A, B, C, D, E = (tid(n) for n in range(6))


def run(coro):
    return asyncio.run(coro)


def make(spec, source, store):
    mpv = FakeMpv()
    return mpv, Radio(mpv, spec, store=store, source=source)


@pytest.fixture
def store(isolated):
    return Store()


async def started(spec, source, store):
    mpv, radio = make(spec, source, store)
    await radio.start()
    await radio.settle()
    return mpv, radio


async def load(mpv, radio, index):
    """mpv switched to an entry and loaded it."""
    mpv.play(index)
    await radio.handle({"event": "start-file", "playlist_entry_id": mpv.entries[index]["id"]})
    await radio.handle({"event": "file-loaded"})
    await radio.settle()


def test_file_options_count_bytes():
    text = "Тест, a=b"
    options = file_options(**{"force-media-title": text, "user-agent": "", "x": "1"})
    assert options == f"force-media-title=%{len(text.encode())}%{text},x=%1%1"
    assert parse_options(options) == {"force-media-title": text, "x": "1"}


def test_launch_spec_round_trip():
    spec = LaunchSpec(
        seed=S,
        stream=FakeSource().stream(S),
        queue=[Track(S, "Ы"), Track(A, "a")],
        source="liked playlist",
        volume=40,
        loop_file=True,
    )
    assert LaunchSpec.from_json(spec.to_json()) == spec
    assert LaunchSpec.from_json(LaunchSpec(seed=S).to_json()) == LaunchSpec(seed=S)


def test_start_fills_the_queue_and_prefetches_the_next_track(store):
    source = FakeSource({S: [S, A, B, C]})
    spec = LaunchSpec(seed=S, stream=source.stream(S))

    mpv, radio = run(started(spec, source, store))

    assert mpv.filenames() == [
        "https://stream/" + S,
        "https://stream/" + A,
        track_url(B),
        track_url(C),
    ]
    assert mpv.pos == 0
    # the prefetched entry is titled from the mix, which knows the artist
    assert mpv.entries[1]["title"] == source.title(A)
    assert source.resolved == [A]
    assert mpv.props[SOURCE_PROPERTY] == "radio mix"
    # the seed is remembered with its title from the mix
    assert store.recent_seeds() == [Track(S, source.title(S))]
    assert mpv.props[LIST_PROPERTY] == [
        {"entry": mpv.entries[0]["id"], "id": S, "title": source.title(S), "current": True}
    ]


def test_without_a_stream_the_seed_goes_through_ytdl(store):
    source = FakeSource({S: [A]})
    mpv, radio = run(started(LaunchSpec(seed=S), source, store))
    assert mpv.filenames()[0] == track_url(S)
    # not in its own mix: the seed is remembered with the player's title
    assert store.recent_seeds() == [Track(S, track_url(S))]


def test_playing_logs_and_refills_near_the_end(store):
    source = FakeSource({S: [S, A, B, C], A: [A, B, D, E]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)
        await load(mpv, radio, 1)
        return mpv, radio

    mpv, radio = run(scenario())
    assert [t.id for t in store.recent_plays()] == [A, S]
    # 2 tracks were left after A: its mix was fetched, B was already queued
    assert source.mixed == [S, A]
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B, C, D, E]
    assert mpv.filenames()[2] == "https://stream/" + B  # the next one is ready in advance
    items = mpv.props[LIST_PROPERTY]
    assert [(it["id"], it["current"]) for it in items] == [(S, False), (A, True)]


def test_played_entries_are_trimmed(store, monkeypatch):
    monkeypatch.setattr(radio_module, "KEEP_BEHIND", 1)
    source = FakeSource({S: [S, A, B, C, D, E]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        for i in range(3):
            await load(mpv, radio, i)
        await load(mpv, radio, mpv.pos + 1)
        return mpv, radio

    mpv, radio = run(scenario())
    # after each step only one played entry stays behind the current one
    assert [radio.video_id(f) for f in mpv.filenames()] == [B, C, D, E]
    assert mpv.pos == 1


def test_everything_played_hooks_on_to_the_last_track(store):
    source = FakeSource({S: [S, A, B], A: [A, B, S], B: [B, C]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 1)
        return mpv, radio

    mpv, radio = run(scenario())
    assert source.mixed == [S, A, B]
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B, C]


def test_refused_stream_is_retried_then_left_to_ytdl(store):
    source = FakeSource({S: [S, A, B]}, bad={A})
    mpv, radio = run(started(LaunchSpec(seed=S, stream=source.stream(S)), source, store))
    assert source.resolved == [A, A]
    assert mpv.filenames()[1] == track_url(A)


def test_expired_stream_falls_back_to_ytdl(store):
    source = FakeSource({S: [S, A, B, C]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 1)
        failed = mpv.entries[1]["id"]
        mpv.play(2)  # after an error mpv moves on by itself
        await radio.handle({"event": "end-file", "reason": "error", "playlist_entry_id": failed})
        return mpv, radio

    mpv, radio = run(scenario())
    assert mpv.filenames()[1] == track_url(A)
    assert mpv.pos == 1  # and it's playing again
    # it had played: after the swap it still shows in the list
    assert mpv.entries[1]["id"] in radio.played


def test_a_ytdl_entry_failing_at_the_end_refills(store):
    source = FakeSource({S: [S], A: [A, B]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await radio.loadfile(track_url(A), "append")
        radio.seen.add(A)  # queued tracks are always seen
        mpv.play(1)
        await radio.handle(
            {"event": "end-file", "reason": "error", "playlist_entry_id": mpv.entries[1]["id"]}
        )
        await radio.settle()
        return mpv, radio

    mpv, radio = run(scenario())
    assert source.mixed == [S, A]
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B]


def test_jump_back_keeps_the_queue(store):
    source = FakeSource({S: [S, A, B, C]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)
        await load(mpv, radio, 1)
        await load(mpv, radio, 2)
        await radio.handle(
            {"event": "client-message", "args": ["musictty-jump", str(mpv.entries[0]["id"])]}
        )
        await load(mpv, radio, 0)
        return mpv, radio

    mpv, radio = run(scenario())
    assert mpv.pos == 0
    # tracks played "ahead" of the current one still show
    items = mpv.props[LIST_PROPERTY]
    assert [(it["id"], it["current"]) for it in items] == [(S, True), (A, False), (B, False)]


def test_liked_playlist_loops_without_mixes(store):
    source = FakeSource({S: [S, A, B]})
    queue = [Track(S, "liked S"), Track(A, "liked A"), Track(B, "liked B")]
    spec = LaunchSpec(seed=S, queue=queue, source="liked playlist")

    async def scenario():
        mpv, radio = await started(spec, source, store)
        await load(mpv, radio, 0)
        await load(mpv, radio, 1)
        await load(mpv, radio, 2)
        return mpv, radio

    mpv, radio = run(scenario())
    assert source.mixed == []
    assert store.recent_seeds() == []  # not a radio
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B]
    # at the last track the next one is the first: the playlist loops
    assert source.resolved == [A, B, S]
    assert mpv.entries[2]["title"] == "liked B"
    assert mpv.props[SOURCE_PROPERTY] == "liked playlist"


def test_a_late_file_loaded_is_about_the_entry_that_loaded(store):
    source = FakeSource({S: [S, A, B, C]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await radio.handle({"event": "start-file", "playlist_entry_id": mpv.entries[0]["id"]})
        mpv.play(1)  # the user pressed next before the radio got to the event
        await radio.handle({"event": "file-loaded"})
        await radio.settle()
        return mpv, radio

    run(scenario())
    assert [t.id for t in store.recent_plays()] == [S]
