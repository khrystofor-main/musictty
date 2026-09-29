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
from musictty.radio import LIST_PROPERTY, LaunchSpec, Radio
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

        await until(lambda: prefetched(A))
        assert await current() == S
        assert await command("now") == (0, f"{source.title(S)} · radio mix\n")

        assert await command("next") == (0, "")
        await until(lambda: prefetched(B))
        assert await current() == A
        assert await command("list") == (0, f" 1. ▶ {source.title(A)}\n 2.   {source.title(S)}\n")

        assert await command("like") == (0, f"♥ {source.title(A)}\n")
        assert await command("repeat", "on") == (0, "")
        assert await command("now") == (0, f"↻ ♥ {source.title(A)} · radio mix\n")
        assert await command("repeat", "off") == (0, "")
        assert await command("vol-") == (0, "")
        assert await mpv.get("volume") == 65

        assert await command("pause") == (0, "")
        assert await mpv.get("pause") is True
        assert await command("play") == (0, "")

        assert await command("list", "back", "2") == (0, "")

        async def back_on_seed():
            return await current() == S

        await until(back_on_seed)

        assert await command("stop") == (0, "")
        await asyncio.wait_for(task, 5)  # the radio ends with its player
        assert proc.wait(timeout=5) == 0

    asyncio.run(scenario())
    assert [t.id for t in store.recent_plays()] == [S, A, S]
    assert store.liked() == [Track(A, source.title(A))]
    assert store.recent_seeds() == [Track(S, source.title(S))]
    assert store.settings().volume == 65


def test_background_radio_starts_replaces_and_stops(files, monkeypatch, capsys):
    # the CLI resolves the seed; the background radio's own YouTube calls just fail here
    monkeypatch.setattr(
        youtube,
        "resolve",
        lambda video_id: Stream(video_id, f"Локально — {video_id}", files[video_id]),
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

    # a new radio replaces the playing one
    assert cli.main([f"https://youtu.be/{A}"]) == 0
    assert now() == f"Локально — {A} · radio mix\n"

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
