"""Against a real mpv (silent, local WAV files instead of YouTube)."""

import asyncio
import os
import shutil
import sys
import time
import wave

import pytest
from fakes import FakeSource, tid

from musictty import cli, paths, player, youtube
from musictty.models import Track
from musictty.radio import LIST_PROPERTY, UPNEXT_PROPERTY, LaunchSpec, Radio
from musictty.store import Store
from musictty.youtube import Stream

# CI sets MUSICTTY_REQUIRE_MPV: there a missing mpv is a failure, not a skip
pytestmark = pytest.mark.skipif(
    not shutil.which("mpv") and not os.environ.get("MUSICTTY_REQUIRE_MPV"), reason="needs mpv"
)

S, A, B = (tid(n) for n in range(3))


@pytest.fixture(autouse=True)
def silent(monkeypatch):
    monkeypatch.setenv("MUSICTTY_MPV_ARGS", "--ao=null")


@pytest.fixture
def files(tmp_path):
    out = {}
    for video_id in (S, A, B):
        path = tmp_path / f"{video_id}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * 8000 * 60)
        out[video_id] = str(path)
    return out


async def until(check, timeout=10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await check():
        assert loop.time() < deadline, "timed out"
        await asyncio.sleep(0.05)


def test_radio_and_commands_on_a_real_player(files, capsys):
    class LocalSource(FakeSource):
        def stream(self, video_id):
            return Stream(video_id, f"player {video_id}", files[video_id])

    source = LocalSource({S: [S, A, B]})
    spec = LaunchSpec(seed=S, stream=source.stream(S))
    store = Store()

    async def command(*argv):
        code = await asyncio.to_thread(cli.main, list(argv))
        return code, capsys.readouterr().out

    async def scenario():
        address = paths.ipc_address()
        proc = player.launch_mpv(spec, address)
        mpv = await player.wait_connect(address, lambda: proc.poll() is None)
        radio = Radio(mpv, spec, store=store, source=source)
        task = asyncio.create_task(radio.run())

        async def current():
            items = await mpv.get(LIST_PROPERTY) or []
            return next((it["id"] for it in items if it["current"]), None)

        async def prefetched(video_id):
            return files[video_id] in [e["filename"] for e in await mpv.get("playlist")]

        async def played(*ids):
            # a track counts once it has loaded: the radio logs it on file-loaded
            return [t.id for t in store.recent_plays()] == list(ids)

        # wait for each track to load before the next step: pressing next while a track
        # is still loading skips it for good (it never played, so it's not in the history)
        await until(lambda: played(S))
        await until(lambda: prefetched(A))
        assert await current() == S
        assert await command("now") == (0, f"{source.title(S)} · radio mix\n")

        assert await command("next") == (0, "")
        await until(lambda: played(A, S))
        await until(lambda: prefetched(B))
        assert await current() == A
        assert await command("list") == (0, f" 1. ▶ {source.title(A)}\n 2.   {source.title(S)}\n")

        assert await command("like") == (0, f"♥ {source.title(A)}\n")
        assert await command("playlists", "new", "Road") == (0, "made Road\n")
        assert await command("playlists", "add", "1") == (0, f"{source.title(A)} → Road\n")
        assert await command("repeat", "on") == (0, "")
        assert await command("now") == (0, f"↻ ♥ {source.title(A)} · radio mix\n")
        assert await command("repeat", "off") == (0, "")
        assert await command("vol-") == (0, "")
        assert await mpv.get("volume") == 65

        code, out = await command("mem")
        assert code == 0
        assert [line.split(":")[0] for line in out.splitlines()] == ["player", "radio"]
        assert "working set" in out

        assert await command("pause") == (0, "")
        assert await mpv.get("pause") is True
        assert await command("play") == (0, "")

        assert await command("list", "back", "2") == (0, "")
        await until(lambda: played(S, A, S))
        assert await current() == S

        # the queue: A once more, right after the current track
        title = source.title

        async def has_format(fmt):
            return await mpv.get("ytdl-format") == fmt

        async def upnext(*ids):
            return [it["id"] for it in await mpv.get(UPNEXT_PROPERTY) or []] == list(ids)

        assert await command("history", "queue", "2") == (0, f"queued: {title(A)}\n")
        await until(lambda: upnext(A, A, B))
        assert await command("upnext") == (0, f" 1. {title(A)}\n 2. {title(A)}\n 3. {title(B)}\n")
        assert await command("upnext", "remove", "1") == (0, "")
        await until(lambda: upnext(A, B))
        assert await command("repeat", "all", "on") == (0, "")
        assert await mpv.get("loop-playlist") == "inf"
        assert await command("now") == (0, f"{title(S)} · radio mix · repeat all\n")
        assert await command("repeat", "all", "off") == (0, "")
        assert await command("seek", "+10") == (0, "")
        assert await mpv.get("time-pos") >= 10
        assert await command("sleep", "30") == (0, "")
        code, out = await command("now")
        assert out.startswith(f"{title(S)} · radio mix · sleep 29:") or "sleep 30:00" in out
        assert await command("sleep", "end") == (0, "")
        assert await command("now") == (0, f"{title(S)} · radio mix · sleep after this track\n")
        assert await command("sleep", "off") == (0, "")
        assert await command("now") == (0, f"{title(S)} · radio mix\n")
        assert await command("quality", "low") == (0, "")
        await until(lambda: has_format("worstaudio[acodec=opus]/worstaudio/worst"))
        assert await command("upnext", "2") == (0, "")  # B now, A still next
        await until(lambda: played(B, S, A, S))
        assert await current() == B
        await until(lambda: upnext(A))
        assert await command("shuffle") == (0, "")

        assert await command("stop") == (0, "")
        await asyncio.wait_for(task, 5)  # the radio ends with its player
        assert proc.wait(timeout=5) == 0

    asyncio.run(scenario())
    assert store.liked() == [Track(A, source.title(A))]
    assert store.recent_seeds() == [Track(S, source.title(S))]
    assert store.settings().volume == 65


def test_background_radio_starts_replaces_and_stops(files, monkeypatch, capsys):
    # the CLI resolves the seed; the background radio's own YouTube calls just fail here
    monkeypatch.setattr(
        youtube,
        "resolve",
        lambda video_id, fmt=None: Stream(video_id, f"Локально — {video_id}", files[video_id]),
    )

    def now():
        # the command returns once the player is up; the first track loads a moment later
        for _ in range(100):
            assert cli.main(["now"]) == 0
            out = capsys.readouterr().out
            if not out.startswith(" · "):
                return out
            time.sleep(0.05)
        return out

    assert cli.main([S]) == 0
    assert now() == f"Локально — {S} · radio mix\n"

    # a new radio replaces the playing one, and keeps its sleep timer
    assert cli.main(["sleep", "30"]) == 0
    for _ in range(100):  # the radio has taken it
        cli.main(["now"])
        if "sleep" in capsys.readouterr().out:
            break
        time.sleep(0.05)
    assert cli.main([f"https://youtu.be/{A}"]) == 0
    out = now()
    assert out.startswith(f"Локально — {A} · radio mix · sleep ")
    assert "sleep 29:" in out or "sleep 30:00" in out

    assert cli.main(["stop"]) == 0
    assert not asyncio.run(player.is_running(paths.ipc_address()))


@pytest.mark.skipif(sys.platform != "win32", reason="console windows are a Windows thing")
def test_background_process_has_no_console_window(tmp_path):
    # the venv's python.exe is a launcher that starts the real interpreter as a child:
    # that child must not get a console window of its own
    code = "import ctypes; print(ctypes.windll.kernel32.GetConsoleWindow())"
    log = tmp_path / "console.log"
    proc = player.spawn_background([sys.executable, "-c", code], b"", log)
    assert proc.wait(timeout=30) == 0
    assert log.read_text().strip() == "0"


def test_the_log_is_appended_and_rotated(tmp_path, monkeypatch):
    log = tmp_path / "radio.log"
    log.write_bytes(b"old radio\n")
    code = "import sys; sys.stdout.write('new radio')"
    assert player.spawn_background([sys.executable, "-c", code], b"", log).wait(timeout=30) == 0
    assert log.read_bytes().replace(b"\r", b"") == b"old radio\nnew radio"

    monkeypatch.setattr(player, "LOG_LIMIT", 5)
    assert player.spawn_background([sys.executable, "-c", code], b"", log).wait(timeout=30) == 0
    assert (tmp_path / "radio.log.1").read_bytes().replace(b"\r", b"") == b"old radio\nnew radio"
    assert log.read_bytes() == b"new radio"


def test_memory_of_a_process():
    private, working_set = player.memory(os.getpid())
    assert working_set > 1_000_000
    if sys.platform == "win32" or sys.platform.startswith("linux"):
        assert 0 < private <= working_set * 4
    assert player.memory(2**22 + 12345) is None  # no such process
