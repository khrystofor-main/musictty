import asyncio
import json
import time

import pytest
from fakes import FakeMpv, FakeSource, parse_options, tid

from musictty import player
from musictty import radio as radio_module
from musictty.models import Track
from musictty.radio import (
    LIST_PROPERTY,
    SOURCE_PROPERTY,
    UPNEXT_PROPERTY,
    LaunchSpec,
    Radio,
    file_options,
    track_url,
)
from musictty.store import Store

S, A, B, C, D, E, F = (tid(n) for n in range(7))


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


def test_album_plays_in_order_then_turns_into_a_radio(store):
    source = FakeSource({C: [C, D, E]})
    queue = [Track(S, "s"), Track(A, "a"), Track(B, "b"), Track(C, "c")]
    spec = LaunchSpec(seed=S, stream=source.stream(S), queue=queue, source="album", then_radio=True)
    assert LaunchSpec.from_json(spec.to_json()) == spec
    assert not spec.loops

    async def scenario():
        mpv, radio = await started(spec, source, store)
        for i in range(3):
            await load(mpv, radio, i)
        assert source.mixed == []  # no mixes while the album plays
        assert mpv.props[SOURCE_PROPERTY] == "album"
        await load(mpv, radio, 3)  # its last track: the radio starts from it
        assert source.mixed == [C]
        await load(mpv, radio, 4)
        return mpv, radio

    mpv, radio = run(scenario())
    # the album's tracks are not queued again
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B, C, D, E]
    assert mpv.props[SOURCE_PROPERTY] == "radio mix"
    assert store.recent_seeds() == []  # an album is not a radio seed


def test_only_the_liked_playlist_loops():
    queue = [Track(S, "s"), Track(A, "a")]
    liked = LaunchSpec(seed=S, queue=queue)
    album = LaunchSpec(seed=S, queue=queue, then_radio=True)
    assert liked.loops
    assert "--loop-playlist=inf" in player.mpv_command("mpv", "addr", liked)
    assert "--loop-playlist=inf" not in player.mpv_command("mpv", "addr", album)
    assert "--loop-playlist=inf" not in player.mpv_command("mpv", "addr", LaunchSpec(seed=S))


# --- the queue ---


def upnext(radio, mpv):
    return [radio.video_id(e["filename"]) for e in mpv.entries[mpv.pos + 1 :]]


async def message(radio, *args):
    await radio.handle({"event": "client-message", "args": [str(a) for a in args]})
    await radio.settle()


def add(where, *ids):
    return ("musictty-add", where, json.dumps([[i, f"added {i}"] for i in ids]))


def test_play_next_and_add_to_queue(store):
    source = FakeSource({S: [S, A, B]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)
        await message(radio, *add("queue", C, D))
        # the queue goes ahead of the radio's own tracks, in order
        assert upnext(radio, mpv) == [C, D, A, B]
        await message(radio, *add("queue", E))
        assert upnext(radio, mpv) == [C, D, E, A, B]  # after what was queued before
        await message(radio, *add("next", F))
        assert upnext(radio, mpv) == [F, C, D, E, A, B]
        # the track that comes next is ready in advance, and it's named as it was added
        assert mpv.entries[1]["filename"] == "https://stream/" + F
        assert mpv.entries[1]["title"] == f"added {F}"
        assert [(it["id"], it["queued"]) for it in mpv.props[UPNEXT_PROPERTY]] == [
            (F, True),
            (C, True),
            (D, True),
            (E, True),
            (A, False),
            (B, False),
        ]
        assert mpv.props[UPNEXT_PROPERTY][1]["title"] == f"added {C}"
        # once it has played, a queued track is just history: new ones go right after the current
        await load(mpv, radio, 1)
        await message(radio, *add("queue", S))
        assert upnext(radio, mpv) == [C, D, E, S, A, B]
        return mpv, radio

    run(scenario())


def test_adding_to_a_finished_queue_plays_it(store):
    source = FakeSource()

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        mpv.current_id = None  # it has run out
        await message(radio, *add("queue", A))
        return mpv, radio

    mpv, radio = run(scenario())
    assert radio.video_id(mpv.entries[mpv.pos]["filename"]) == A


def test_remove_move_and_play_from_up_next(store):
    source = FakeSource({S: [S, A, B, C, D]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)

        def entry(video_id):
            return next(e["id"] for e in mpv.entries if radio.video_id(e["filename"]) == video_id)

        await message(radio, "musictty-remove", entry(B))
        assert upnext(radio, mpv) == [A, C, D]
        await message(radio, "musictty-remove", entry(S))  # the playing track stays
        assert upnext(radio, mpv) == [A, C, D]
        await message(radio, "musictty-move", entry(D), "before", entry(A))
        assert upnext(radio, mpv) == [D, A, C]
        await message(radio, "musictty-move", entry(D), "after", entry(C))
        assert upnext(radio, mpv) == [A, C, D]
        await message(radio, "musictty-move", entry(A), "after", entry(C))
        assert upnext(radio, mpv) == [C, A, D]
        assert not radio.queued
        # moved to the top, a radio's track joins the queue: add to queue goes after it
        await message(radio, "musictty-move", entry(D), "before", entry(C))
        assert radio.queued == {entry(D)}
        await message(radio, "musictty-move", entry(D), "after", entry(A))
        assert not radio.queued
        # the next track is always ready in advance
        assert mpv.entries[1]["filename"] == "https://stream/" + C
        await message(radio, "musictty-play", entry(D))  # the ones before it still come next
        assert radio.video_id(mpv.entries[mpv.pos]["filename"]) == D
        assert upnext(radio, mpv) == [C, A]
        return mpv, radio

    run(scenario())


def test_shuffle_keeps_what_has_played(store, monkeypatch):
    source = FakeSource({S: [S, A, B, C, D, E]})
    monkeypatch.setattr(radio_module.random, "sample", lambda xs, n: list(reversed(xs)))

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)
        await load(mpv, radio, 1)
        await message(radio, *add("queue", F))
        await message(radio, "musictty-shuffle")
        return mpv, radio

    mpv, radio = run(scenario())
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, E, D, C, B, F]
    assert mpv.pos == 1
    assert not radio.queued  # the shuffled queue is one queue: add to queue goes after the current


def loop(on):
    return {"event": "property-change", "name": "loop-playlist", "data": "inf" if on else False}


def test_repeat_all_stops_the_mixes_and_loops(store):
    source = FakeSource({S: [S, A, B, C], C: [C, D, E]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)
        await radio.handle(loop(True))
        for i in (1, 2, 3):
            await load(mpv, radio, i)
        assert source.mixed == [S]  # no mix at the end: the queue starts over
        # up next goes round to the current track
        assert [it["id"] for it in mpv.props[UPNEXT_PROPERTY]] == [S, A, B]
        # repeat all off at the last track: the radio goes on from it
        await radio.handle(loop(False))
        await radio.settle()
        return mpv, radio

    mpv, radio = run(scenario())
    assert source.mixed == [S, C]
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B, C, D, E]


def test_shuffle_with_repeat_all_goes_round(store, monkeypatch):
    source = FakeSource({S: [S, A, B, C]})
    monkeypatch.setattr(radio_module.random, "sample", lambda xs, n: list(reversed(xs)))

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await radio.handle(loop(True))
        await load(mpv, radio, 0)
        await load(mpv, radio, 1)
        await message(radio, "musictty-shuffle")
        return mpv, radio

    mpv, radio = run(scenario())
    # the whole loop after the current track: C, B, then S that had played before it
    assert [radio.video_id(f) for f in mpv.filenames()] == [A, S, C, B]
    assert mpv.pos == 0


def test_queue_after_an_album_plays_before_the_radio(store):
    source = FakeSource({A: [A, C, D], E: [E, D]})
    queue = [Track(S, "s"), Track(A, "a")]
    spec = LaunchSpec(seed=S, stream=source.stream(S), queue=queue, source="album", then_radio=True)

    async def scenario():
        mpv, radio = await started(spec, source, store)
        await load(mpv, radio, 0)
        # added to the queue: after the album, not in the middle of it
        await message(radio, *add("queue", B, E))
        assert upnext(radio, mpv) == [A, B, E]
        await load(mpv, radio, 1)
        assert source.mixed == []  # the queue isn't over yet
        await load(mpv, radio, 2)
        assert mpv.props[SOURCE_PROPERTY] == "album"  # a queued track is not the radio yet
        await load(mpv, radio, 3)
        return mpv, radio

    mpv, radio = run(scenario())
    assert source.mixed == [E]
    assert [radio.video_id(f) for f in mpv.filenames()] == [S, A, B, E, D]


def test_removing_a_track_queued_twice_keeps_the_other(store):
    source = FakeSource({S: [S, A, B, C]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        await load(mpv, radio, 0)
        await message(radio, *add("next", A))  # ready in advance: the same stream URL as A's
        assert mpv.filenames()[1:3] == ["https://stream/" + A] * 2
        await message(radio, "musictty-remove", mpv.entries[1]["id"])
        return mpv, radio

    mpv, radio = run(scenario())
    assert [it["id"] for it in mpv.props[UPNEXT_PROPERTY]] == [A, B, C]


# --- the sleep timer, the audio quality ---


def test_sleep_timer_turns_the_volume_down_and_stops(store, monkeypatch):
    monkeypatch.setattr(radio_module, "FADE_SECONDS", 0.2)
    source = FakeSource({S: [S, A]})
    volumes = []

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        mpv.props["volume"] = 80
        await message(radio, "musictty-sleep", "0.3")
        deadline = float(mpv.props[radio_module.SLEEP_PROPERTY])
        assert 0 < deadline - time.time() <= 0.3
        while not mpv.quit:
            volumes.append(mpv.props["volume"])
            await asyncio.sleep(0.01)
        return mpv

    mpv = run(scenario())
    assert volumes[0] == 80 and any(0 < v < 80 for v in volumes)  # down, then it stops
    assert mpv.props["volume"] == 0


def test_a_new_sleep_timer_replaces_the_old_one(store, monkeypatch):
    monkeypatch.setattr(radio_module, "FADE_SECONDS", 1.0)
    source = FakeSource({S: [S, A]})

    async def scenario():
        mpv, radio = await started(LaunchSpec(seed=S, stream=source.stream(S)), source, store)
        mpv.props["volume"] = 80
        await message(radio, "musictty-sleep", "1")
        await asyncio.sleep(0.3)  # fading already
        assert mpv.props["volume"] < 80
        await message(radio, "musictty-sleep", "0")  # off: the volume comes back
        await asyncio.sleep(0.05)
        assert mpv.props["volume"] == 80 and mpv.props[radio_module.SLEEP_PROPERTY] == ""
        await message(radio, "musictty-sleep", "end")
        assert mpv.props[radio_module.SLEEP_PROPERTY] == "end"
        await asyncio.sleep(1.1)
        assert not mpv.quit  # the earlier timer is gone
        await radio.handle({"event": "end-file", "reason": "stop"})  # next: not the end
        assert not mpv.quit
        await radio.handle({"event": "end-file", "reason": "eof"})  # the track is over
        return mpv

    assert run(scenario()).quit


def test_audio_quality(store):
    source = FakeSource({S: [S, A, B]})
    spec = LaunchSpec(seed=S, stream=source.stream(S), quality="low")
    assert LaunchSpec.from_json(spec.to_json()) == spec
    assert "--ytdl-format=worstaudio[acodec=opus]/worstaudio/worst" in player.mpv_command(
        "mpv", "addr", spec
    )

    async def scenario():
        mpv, radio = await started(spec, source, store)
        await message(radio, "musictty-quality", "high")
        await load(mpv, radio, 1)
        await message(radio, "musictty-quality", "bogus")  # ignored
        return mpv

    mpv = run(scenario())
    # the next track was resolved at the start; the ones after the change in the new quality
    assert source.formats[0] == "worstaudio[acodec=opus]/worstaudio/worst"
    assert set(source.formats[1:]) == {"bestaudio/best"}
    assert mpv.props["ytdl-format"] == "bestaudio/best"
