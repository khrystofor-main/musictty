"""The musictty command. Every call is a short-lived process; the radio itself runs in the
background (see daemon.py) and is reached over mpv's IPC.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from . import control, player, youtube
from .control import Failure, list_lines
from .ipc import Mpv, MpvError
from .models import Track
from .radio import (
    ADD_TO_QUEUE,
    JUMP_MESSAGE,
    LIST_PROPERTY,
    PID_PROPERTY,
    PLAY_MESSAGE,
    PLAY_NEXT,
    PLAYLIST,
    RADIO_MIX,
    REMOVE_MESSAGE,
    SHUFFLE_MESSAGE,
    SLEEP_END,
    SOURCE_PROPERTY,
)
from .store import (
    LIKED,
    PLAYS,
    SEEDS,
    SETTINGS,
    AlreadyImported,
    PlaylistError,
    Store,
    find_v0_dir,
    import_v0,
)

HELP = """\
musictty — endless music radio in the terminal

  musictty search <query>      new radio from a track found on YouTube Music
  musictty <link>              new radio from a track link
                               (music.youtube.com, youtube.com, youtu.be)
  musictty ai <mood>           the ai picks songs for a mood, then the radio goes on
                               (key in DEEPSEEK_API_KEY or NOUS_API_KEY)

  musictty                     the player: now playing, radios, history, liked, search
  musictty recent              recent radios
  musictty <n>                 start recent radio n again

  musictty list                current and played tracks (▶ current, ↻ on repeat, ♥ liked)
  musictty list <n>            new radio from track n
  musictty list back <n>       jump back to track n (the queue is kept)
  musictty upnext              tracks coming up: the queue, then the radio's picks
  musictty upnext <n>          play track n now (the ones before it still come next)
  musictty upnext remove <n>   remove track n from what's coming up
  musictty shuffle             shuffle what's coming up
  musictty history             last 30 played tracks of all radios
  musictty history <n>         new radio from track n
  musictty liked               liked tracks
  musictty liked <n>           new radio from liked track n
  musictty liked play <n>      play liked tracks in a loop, from track n down
  musictty liked repeat <n>    same, with track n on repeat
  musictty liked remove <n>    remove track n from liked
  musictty history next <n>    play track n next (liked next <n> too)
  musictty history queue <n>   add track n to the queue (liked queue <n> too)

  musictty playlists           your playlists
  musictty playlists <n>       the tracks of playlist n
  musictty playlists play <n>  play playlist n, then a radio
  musictty playlists add <n>   add the current track to playlist n
  musictty playlists new <name>
                               make a playlist
  musictty playlists delete <n>
                               delete playlist n

  musictty like                like the current track
  musictty unlike              remove the current track from liked
  musictty now                 what's playing and from where (radio mix or playlist)
  musictty next                next track
  musictty prev                previous track
  musictty pause               pause
  musictty play                resume
  musictty repeat on           repeat the current track
  musictty repeat off          stop repeating
  musictty repeat all on|off   repeat the whole queue (the radio stops adding tracks)
  musictty seek <±seconds>     seek in the track: seek +10, seek -10
  musictty sleep <minutes>     stop the radio in n minutes (the sound fades out)
  musictty sleep end           stop it after the track that's playing
  musictty sleep off           no sleep timer
  musictty quality             the audio quality: low, normal or high
  musictty quality <level>     set it (for the tracks to come, and the next radios)
  musictty vol+ / vol-         volume ±5
  musictty stop                stop the radio

  musictty mem                 memory of the player and the radio
  musictty update              update yt-dlp if the radio stops finding tracks
  musictty import-v0 [folder]  import radios, history and likes from the PowerShell version
  musictty help                this list"""

SIMPLE = {
    "recent",
    "now",
    "next",
    "prev",
    "pause",
    "play",
    "vol+",
    "vol-",
    "stop",
    "like",
    "unlike",
    "update",
    "mem",
    "help",
    "import-v0",
    "list",
    "history",
    "liked",
    "upnext",
    "shuffle",
    "playlists",
    "quality",
}
QUEUE_ACTIONS = {PLAY_NEXT, ADD_TO_QUEUE}
LIST_ACTIONS = {
    "list": {"back"},
    "history": QUEUE_ACTIONS,
    "liked": {"play", "repeat", "remove", *QUEUE_ACTIONS},
    "upnext": {"remove"},
    "playlists": {"play", "add", "delete"},
}
LINK = re.compile(r"https?://([\w-]+\.)*(youtube\.com|youtu\.be)/", re.ASCII)
NUMBER = re.compile(r"[1-9][0-9]{0,3}")
SECONDS = re.compile(r"[+-]?[0-9]{1,4}")

T = TypeVar("T")


class Invalid(Exception):
    """Not a command: nothing is touched."""


@dataclass(frozen=True)
class Call:
    name: str
    text: str | None = None  # search query, link or id; on/off; a folder
    number: int | None = None  # 1-based position in a printed list; seconds to seek
    action: str | None = None  # search; list back; liked play / repeat / remove; repeat all


def _number(text: str) -> int | None:
    return int(text) if NUMBER.fullmatch(text) else None


def parse(argv: list[str]) -> Call:
    """Strict: only what's in the help passes; everything else is invalid input."""
    if not argv:
        return Call("ui")
    head, rest = argv[0], argv[1:]
    if head == "search":
        if not rest:
            raise Invalid
        return Call("radio", text=" ".join(rest), action="search")
    if head == "ai":
        if not rest:
            raise Invalid
        return Call("ai", text=" ".join(rest))
    if not rest:
        if head in SIMPLE:
            return Call(head)
        if (n := _number(head)) is not None:
            return Call("recent", number=n)
        if LINK.match(head) or youtube.is_video_id(head):
            return Call("radio", text=head)
        raise Invalid
    if head == "repeat" and rest in (["on"], ["off"]):
        return Call("repeat", text=rest[0])
    if head == "repeat" and rest in (["all", "on"], ["all", "off"]):
        return Call("repeat", text=rest[1], action="all")
    if head == "sleep" and len(rest) == 1:
        if rest[0] in (SLEEP_END, "off"):
            return Call("sleep", text=rest[0])
        if (n := _number(rest[0])) is not None:
            return Call("sleep", number=n)
    if head == "quality" and len(rest) == 1 and rest[0] in youtube.QUALITIES:
        return Call("quality", text=rest[0])
    if head == "seek" and len(rest) == 1 and SECONDS.fullmatch(rest[0]):
        return Call("seek", number=int(rest[0]))
    if head == "playlists" and rest[0] == "new" and len(rest) > 1:
        return Call("playlists", text=" ".join(rest[1:]), action="new")
    if head == "import-v0" and len(rest) == 1:
        return Call(head, text=rest[0])
    if head in LIST_ACTIONS:
        if len(rest) == 1 and (n := _number(rest[0])) is not None:
            return Call(head, number=n)
        if len(rest) == 2 and rest[0] in LIST_ACTIONS[head] and (n := _number(rest[1])) is not None:
            return Call(head, number=n, action=rest[0])
    raise Invalid


# --- talking to the radio ---


def on_player(fn: Callable[[Mpv], Awaitable[T]]) -> T:
    """Run fn against the playing radio; NotRunning if there is none."""
    return asyncio.run(control.with_player(fn))


def pick(items: list[T], number: int) -> T:
    if number > len(items):
        raise Invalid
    return items[number - 1]


def print_numbered(titles: list[str]) -> None:
    for n, title in enumerate(titles, 1):
        print(f"{n:2}. {title}")


def start_radio(
    seed: str | None = None,
    *,
    query: str | None = None,
    queue: list[Track] | None = None,
    repeat_one: bool = False,
) -> None:
    asyncio.run(control.start(seed, query=query, queue=queue, repeat_one=repeat_one))


# --- commands ---


def cmd_help(call: Call) -> None:
    print(HELP)


def cmd_ui(call: Call) -> int | None:
    # the player needs a terminal; redirected, it's the list of recent radios, as in v0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return cmd_recent(Call("recent"))
    from .tui import run

    run()
    return None


def cmd_recent(call: Call) -> None:
    seeds = Store().recent_seeds(10)
    if call.number is None:
        print_numbered([t.title for t in seeds])
    else:
        start_radio(pick(seeds, call.number).id)


def cmd_ai(call: Call) -> None:
    assert call.text
    tracks = asyncio.run(control.ai_radio(call.text))
    print_numbered([t.title for t in tracks])


def cmd_radio(call: Call) -> None:
    assert call.text
    if call.action == "search":
        start_radio(query=call.text)
        return
    video_id = call.text if youtube.is_video_id(call.text) else youtube.video_id_from_url(call.text)
    if not video_id:
        raise Failure("no track in this link")
    start_radio(video_id)


def cmd_now(call: Call) -> None:
    liked = Store().liked_ids()

    async def now(mpv: Mpv) -> str:
        # the title from the radio's list; until it's there, from the player
        cur = await control.current_track(mpv)
        title = (cur or {}).get("title") or await mpv.get("media-title") or ""
        mark = "↻ " if await control.repeating(mpv) else ""
        if cur and cur.get("id") in liked:
            mark += "♥ "
        source = await mpv.get(SOURCE_PROPERTY) or RADIO_MIX
        if await control.repeating_all(mpv):
            source += " · repeat all"
        if sleep := await control.sleep_status(mpv):
            source += f" · {sleep}"
        return f"{mark}{title} · {source}"

    print(on_player(now))


def cmd_like(call: Call) -> int | None:
    cur = on_player(control.current_track)
    if not cur or not cur.get("id"):
        return 1
    track = Track(cur["id"], cur.get("title") or cur["id"])
    if call.name == "like":
        Store().like(track)
        print(f"♥ {track.title}")
    else:
        Store().unlike(track.id)
        print(f"♡ {track.title}")
    return None


def cmd_list(call: Call) -> int | None:
    async def read(mpv: Mpv) -> tuple[list[dict], bool]:
        # newest on top; after going back, the tracks played "ahead" show above the current one
        return list(reversed(await mpv.get(LIST_PROPERTY) or [])), await control.repeating(mpv)

    items, repeat = on_player(read)
    if call.number is None:
        for line in list_lines(items, repeat, Store().liked_ids()):
            print(line)
        return None
    item = pick(items, call.number)
    if call.action == "back":
        if not item.get("current"):
            on_player(lambda mpv: mpv.command("script-message", JUMP_MESSAGE, str(item["entry"])))
        return None
    if not item.get("id"):
        return 1
    start_radio(item["id"])
    return None


def enqueue(track: Track, where: str) -> None:
    if asyncio.run(control.enqueue([track], where)):
        print(f"{'next' if where == PLAY_NEXT else 'queued'}: {track.title}")


def cmd_history(call: Call) -> None:
    plays = Store().recent_plays(30)
    if call.number is None:
        print_numbered([t.title for t in plays])
    elif call.action in QUEUE_ACTIONS:
        enqueue(pick(plays, call.number), call.action)
    else:
        start_radio(pick(plays, call.number).id)


def cmd_liked(call: Call) -> None:
    store = Store()
    items = list(reversed(store.liked()))  # newest first
    if call.number is None:
        print_numbered([t.title for t in items])
        return
    track = pick(items, call.number)
    if call.action == "remove":
        store.unlike(track.id)
        print(f"♡ {track.title}")
    elif call.action in QUEUE_ACTIONS:
        enqueue(track, call.action)
    elif call.action is None:
        start_radio(track.id)
    else:
        queue = control.liked_queue(items, call.number - 1)
        start_radio(track.id, queue=queue, repeat_one=call.action == "repeat")


def cmd_upnext(call: Call) -> None:
    items = on_player(control.upnext)
    if call.number is None:
        print_numbered([it["title"] for it in items])
        return
    item = pick(items, call.number)
    name = REMOVE_MESSAGE if call.action == "remove" else PLAY_MESSAGE
    asyncio.run(control.message(name, str(item["entry"])))


def cmd_playlists(call: Call) -> None:
    store = Store()
    if call.action == "new":
        assert call.text
        print(f"made {store.create_playlist(call.text).name}")
        return
    playlists = store.playlists()
    if call.number is None:
        print_numbered([f"{p.name} ({len(p.tracks)})" for p in playlists])
        return
    playlist = pick(playlists, call.number)
    if call.action is None:
        print_numbered([t.title for t in playlist.tracks])
    elif call.action == "delete":
        store.delete_playlist(playlist.name)
        print(f"deleted {playlist.name}")
    elif call.action == "add":
        cur = on_player(control.current_track)
        if not cur or not cur.get("id"):
            raise Failure("nothing is playing")
        track = Track(cur["id"], cur.get("title") or cur["id"])
        added = store.add_to_playlist(playlist.name, [track])
        print(f"{track.title} → {playlist.name}" if added else f"already in {playlist.name}")
    elif not playlist.tracks:
        raise Failure(f"{playlist.name} is empty")
    else:
        tracks = playlist.tracks
        asyncio.run(control.start(tracks[0].id, queue=tracks, then_radio=True, source=PLAYLIST))


def cmd_shuffle(call: Call) -> None:
    asyncio.run(control.message(SHUFFLE_MESSAGE))


def cmd_repeat(call: Call) -> None:
    if call.action == "all":
        asyncio.run(control.set_repeat_all(call.text == "on"))
        return
    asyncio.run(control.set_repeat(call.text == "on"))


def cmd_sleep(call: Call) -> None:
    if call.text == "off":
        when = "0"
    elif call.text == SLEEP_END:
        when = SLEEP_END
    else:
        assert call.number is not None
        when = str(call.number * 60)
    asyncio.run(control.set_sleep(when))


def cmd_quality(call: Call) -> None:
    if call.text is None:
        print(Store().settings().quality)
        return
    asyncio.run(control.set_quality(call.text))


def cmd_seek(call: Call) -> None:
    assert call.number is not None
    asyncio.run(control.seek(call.number))


def cmd_volume(call: Call) -> None:
    asyncio.run(control.change_volume(5 if call.name == "vol+" else -5))


def cmd_playback(call: Call) -> None:
    command: tuple[Any, ...] = {
        "next": ("playlist-next", "force"),
        "prev": ("playlist-prev",),
        "pause": ("set_property", "pause", True),
        "play": ("set_property", "pause", False),
    }[call.name]
    on_player(lambda mpv: mpv.command(*command))


def cmd_stop(call: Call) -> None:
    asyncio.run(control.stop())


def cmd_mem(call: Call) -> None:
    async def pids(mpv: Mpv) -> tuple[int | None, int | None]:
        return await mpv.get("pid"), await mpv.get(PID_PROPERTY)

    for name, pid in zip(("player", "radio"), on_player(pids), strict=True):
        usage = player.memory(pid) if pid else None
        if usage:
            private, working_set = usage
            parts = [f"private {private / 2**20:.1f} mb"] if private is not None else []
            parts.append(f"working set {working_set / 2**20:.1f} mb")
            print(f"{name}: " + ", ".join(parts))


def cmd_update(call: Call) -> int:
    # YouTube breaks old yt-dlp versions now and then; the running radio picks it up on restart
    package = "yt-dlp[default,deno]"
    if importlib.util.find_spec("pip"):
        command = [sys.executable, "-m", "pip", "install", "--upgrade", package]
    elif uv := shutil.which("uv"):
        command = [uv, "pip", "install", "--python", sys.executable, "--upgrade", package]
    else:
        raise Failure(
            "pip is not available here: upgrade yt-dlp with the tool that installed musictty"
        )
    return subprocess.run(command).returncode


def cmd_import(call: Call) -> None:
    src = Path(call.text) if call.text else find_v0_dir()
    if not src:
        raise Failure("v0 folder not found, pass it: musictty import-v0 <folder>")
    if not src.is_dir():
        raise Failure(f"not a folder: {src}")
    try:
        counts = import_v0(Store(), src)
    except AlreadyImported:
        raise Failure("already imported") from None
    if not counts:
        print(f"nothing to import in {src}")
        return
    parts = [
        f"{counts.get(SEEDS, 0)} radios",
        f"{counts.get(PLAYS, 0)} plays",
        f"{counts.get(LIKED, 0)} liked",
    ]
    if counts.get(SETTINGS):
        parts.append("volume and repeat")
    print("imported: " + ", ".join(parts))


COMMANDS: dict[str, Callable[[Call], int | None]] = {
    "help": cmd_help,
    "ui": cmd_ui,
    "recent": cmd_recent,
    "radio": cmd_radio,
    "ai": cmd_ai,
    "now": cmd_now,
    "like": cmd_like,
    "unlike": cmd_like,
    "list": cmd_list,
    "history": cmd_history,
    "liked": cmd_liked,
    "upnext": cmd_upnext,
    "shuffle": cmd_shuffle,
    "playlists": cmd_playlists,
    "repeat": cmd_repeat,
    "seek": cmd_seek,
    "sleep": cmd_sleep,
    "quality": cmd_quality,
    "vol+": cmd_volume,
    "vol-": cmd_volume,
    "next": cmd_playback,
    "prev": cmd_playback,
    "pause": cmd_playback,
    "play": cmd_playback,
    "stop": cmd_stop,
    "update": cmd_update,
    "mem": cmd_mem,
    "import-v0": cmd_import,
}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # ♥ ▶ ↻ on a console that can't show them
    try:
        call = parse(sys.argv[1:] if argv is None else argv)
        return COMMANDS[call.name](call) or 0
    except Invalid:
        print("invalid input")
    except (Failure, PlaylistError) as e:
        print(e)
    except MpvError:
        pass  # the radio is off (or went away): quietly, like v0
    except KeyboardInterrupt:
        return 130
    return 1
