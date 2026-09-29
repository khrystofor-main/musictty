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

from . import control, youtube
from .control import Failure, list_lines
from .ipc import Mpv, MpvError
from .models import Track
from .radio import (
    JUMP_MESSAGE,
    LIST_PROPERTY,
    RADIO_MIX,
    SOURCE_PROPERTY,
)
from .store import (
    LIKED,
    PLAYS,
    SEEDS,
    SETTINGS,
    AlreadyImported,
    Store,
    find_v0_dir,
    import_v0,
)

HELP = """\
musictty — endless music radio in the terminal

  musictty search <query>      new radio from a track found on YouTube Music
  musictty <link>              new radio from a track link
                               (music.youtube.com, youtube.com, youtu.be)

  musictty                     the player: now playing, radios, history, liked, search
  musictty recent              recent radios
  musictty <n>                 start recent radio n again

  musictty list                current and played tracks (▶ current, ↻ on repeat, ♥ liked)
  musictty list <n>            new radio from track n
  musictty list back <n>       jump back to track n (the queue is kept)
  musictty history             last 30 played tracks of all radios
  musictty history <n>         new radio from track n
  musictty liked               liked tracks
  musictty liked <n>           new radio from liked track n
  musictty liked play <n>      play liked tracks in a loop, from track n down
  musictty liked repeat <n>    same, with track n on repeat
  musictty liked remove <n>    remove track n from liked

  musictty like                like the current track
  musictty unlike              remove the current track from liked
  musictty now                 what's playing and from where (radio mix or playlist)
  musictty next                next track
  musictty prev                previous track
  musictty pause               pause
  musictty play                resume
  musictty repeat on           repeat the current track
  musictty repeat off          stop repeating
  musictty vol+ / vol-         volume ±5
  musictty stop                stop the radio

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
    "help",
    "import-v0",
    "list",
    "history",
    "liked",
}
LIST_ACTIONS = {"list": {"back"}, "history": set(), "liked": {"play", "repeat", "remove"}}
LINK = re.compile(r"https?://([\w-]+\.)*(youtube\.com|youtu\.be)/", re.ASCII)
NUMBER = re.compile(r"[1-9][0-9]{0,3}")

T = TypeVar("T")


class Invalid(Exception):
    """Not a command: nothing is touched."""


@dataclass(frozen=True)
class Call:
    name: str
    text: str | None = None  # search query, link or id; on/off; a folder
    number: int | None = None  # 1-based position in a printed list
    action: str | None = None  # search; list back; liked play / repeat / remove


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


def cmd_history(call: Call) -> None:
    plays = Store().recent_plays(30)
    if call.number is None:
        print_numbered([t.title for t in plays])
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
    elif call.action is None:
        start_radio(track.id)
    else:
        queue = control.liked_queue(items, call.number - 1)
        start_radio(track.id, queue=queue, repeat_one=call.action == "repeat")


def cmd_repeat(call: Call) -> None:
    asyncio.run(control.set_repeat(call.text == "on"))


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
    "now": cmd_now,
    "like": cmd_like,
    "unlike": cmd_like,
    "list": cmd_list,
    "history": cmd_history,
    "liked": cmd_liked,
    "repeat": cmd_repeat,
    "vol+": cmd_volume,
    "vol-": cmd_volume,
    "next": cmd_playback,
    "prev": cmd_playback,
    "pause": cmd_playback,
    "play": cmd_playback,
    "stop": cmd_stop,
    "update": cmd_update,
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
    except Failure as e:
        print(e)
    except MpvError:
        pass  # the radio is off (or went away): quietly, like v0
    except KeyboardInterrupt:
        return 130
    return 1
