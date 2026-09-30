"""The terminal UI: `musictty` with no arguments.

Just another client of mpv's IPC, like the CLI: it watches the player and the track list the
radio publishes, and starts radios through control.start. Quitting it leaves the music playing.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Input, OptionList, ProgressBar, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from . import control, music, paths, player
from .control import Failure
from .ipc import Mpv, MpvError, NotRunning
from .models import Track
from .radio import (
    ADD_TO_QUEUE,
    ALBUM,
    JUMP_MESSAGE,
    LIST_PROPERTY,
    MOVE_MESSAGE,
    PLAY_MESSAGE,
    PLAY_NEXT,
    RADIO_MIX,
    REMOVE_MESSAGE,
    SHUFFLE_MESSAGE,
    SOURCE_PROPERTY,
    UPNEXT_PROPERTY,
)
from .store import Store

OBSERVED = ("media-title", "pause", "volume", "loop-file", "loop-playlist", "duration")
OBSERVED += (LIST_PROPERTY, SOURCE_PROPERTY, UPNEXT_PROPERTY)

# tab id -> title; keys 1-7 switch between them
TABS = {
    "radio": "Radio",
    "recent": "Recent",
    "history": "History",
    "liked": "Liked",
    "search": "Search",
    "lyrics": "Lyrics",
    "upnext": "Up next",
}
SEEK_STEP = 10  # seconds

LYRICS_TOP = 2  # the track's title and a blank line above the lyrics


SEARCH_HINT = "/ search YouTube Music: songs, albums, artists"
AI_HINT = "a · describe a mood, the AI picks songs to start a radio"


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


class TrackList(OptionList):
    """Tracks of one tab. `rows` are the radio's list items (dicts), Tracks or search Results;
    None for a heading."""

    def __init__(self, kind: str):
        super().__init__(markup=False, id=f"{kind}-list")
        self.kind = kind
        self.rows: list[Any] = []

    def show(self, rows: list[Any], lines: list[str], keep: Any = None, key=None) -> None:
        """Replace the rows, keeping the cursor on the same row when it's still there."""
        old = self.highlighted
        self.rows = rows
        self.set_options(lines)
        if not rows:
            return
        if key and keep is not None:
            at = next((i for i, row in enumerate(rows) if key(row) == keep), None)
            if at is not None:
                self.highlighted = at
                return
        self.highlighted = min(old or 0, len(rows) - 1)


def heading(text: str) -> Option:
    return Option(Text(text, style="bold"), disabled=True)


def result_line(result: music.Result) -> Text:
    return Text.assemble(result.title, (f"  {result.detail}" if result.detail else "", "dim"))


def row_id(row: Any) -> str:
    return (row.get("id") or "") if isinstance(row, dict) else row.id


def row_title(row: Any) -> str:
    return row.get("title", "?") if isinstance(row, dict) else row.title


# the key bar: the player's keys, then those of the focused list
PLAYER_KEYS = [
    ("space", "pause"),
    ("n", "next"),
    ("p", "prev"),
    ("+/-", "volume"),
    (",/.", "seek"),
    ("l", "like"),
    ("r/R", "repeat one/all"),
    ("x", "shuffle"),
    ("s", "stop"),
]
QUEUE_KEYS = [("e", "queue"), ("E", "play next")]
LIST_KEYS = {
    "radio": [("enter", "radio"), ("←", "jump back"), *QUEUE_KEYS],
    "recent": [("enter", "radio"), *QUEUE_KEYS],
    "history": [("enter", "radio"), *QUEUE_KEYS],
    "liked": [
        ("enter", "radio"),
        ("→/←", "loop/+repeat"),
        ("del", "unlike"),
        *QUEUE_KEYS,
    ],
    "search": [("enter", "play/open"), ("←", "back"), *QUEUE_KEYS],
    "lyrics": [],
    "upnext": [
        ("enter", "play now"),
        ("E", "to the top"),
        ("shift+↑/↓", "move"),
        ("del", "remove"),
    ],
}
APP_KEYS = [("/", "search"), ("a", "ai radio"), ("q", "quit")]
INPUT_KEYS = [("enter", "go"), ("esc", "back")]


def key_line(keys: list[tuple[str, str]]) -> str:
    return "  ".join(f"[b $footer-key-foreground]{key}[/] {desc}" for key, desc in keys)


class MusicApp(App):
    TITLE = "musictty"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    #player { height: auto; padding: 1 2; }
    #now { height: auto; }
    #progress { width: 1fr; }
    #progress Bar { width: 1fr; }
    TabbedContent { height: 1fr; }
    TrackList { height: 1fr; border: none; }
    #search { margin: 0 1; }
    #keys { height: auto; padding: 0 1; background: $footer-background; }
    """

    BINDINGS = [
        Binding("space", "pause", "pause"),
        Binding("n", "next", "next"),
        Binding("p", "prev", "prev"),
        Binding("plus,equals_sign", "volume(5)", "vol+", key_display="+"),
        Binding("minus", "volume(-5)", "vol-", key_display="-"),
        Binding("comma", f"seek({-SEEK_STEP})", "-10s", key_display=","),
        Binding("full_stop", f"seek({SEEK_STEP})", "+10s", key_display="."),
        Binding("l", "like", "like"),
        Binding("r", "repeat", "repeat"),
        Binding("R,shift+r", "repeat_all", "repeat all", key_display="R"),
        Binding("x", "shuffle", "shuffle"),
        Binding("s", "stop", "stop"),
        Binding("e", f"enqueue('{ADD_TO_QUEUE}')", "queue"),
        Binding("E,shift+e", f"enqueue('{PLAY_NEXT}')", "play next", key_display="E"),
        Binding("shift+up", "move(-1)", "up", show=False),
        Binding("shift+down", "move(1)", "down", show=False),
        Binding("slash", "search", "search", key_display="/"),
        Binding("a", "ai", "ai radio"),
        Binding("q", "quit", "quit"),
        Binding("escape", "back", show=False),
        Binding("left", "row('left')", show=False),
        Binding("right", "row('right')", show=False),
        Binding("delete", "row('delete')", show=False),
        *(Binding(str(n), f"tab('{tab}')", show=False) for n, tab in enumerate(TABS, 1)),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.mpv: Mpv | None = None
        self.props: dict[str, Any] = {}
        self.position: float | None = None
        self.liked_ids: set[str] = set()  # refreshed with the lists
        # the search tab: what an artist's songs replaced, for going back with ←
        self.search_back: list[tuple[list[Any], list[Any]]] = []
        # the lyrics tab: whose lyrics it shows, and the lyrics already fetched
        self.lyrics_id: str | None = ""  # "" = nothing shown yet; None = nothing playing
        self.lyrics_cache: dict[str, music.Lyrics | None] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="player"):
            yield Static(id="now")
            yield ProgressBar(id="progress", show_eta=False, show_percentage=False)
        with TabbedContent(initial="radio"):
            for n, (tab, title) in enumerate(TABS.items(), 1):
                with TabPane(f"{n} {title}", id=tab):
                    yield TrackList(tab)
        yield Input(placeholder=SEARCH_HINT, id="search")
        yield Static(id="keys")

    async def on_mount(self) -> None:
        self.render_now()
        self.refresh_lists()
        # nothing playing: start where v0's bare `music` did, on the recent radios
        playing = await player.is_running(paths.ipc_address())
        self.action_tab("radio" if playing else "recent")
        self.run_worker(self.watch_player(), group="player")
        self.set_interval(0.5, self.poll_position)

    # --- the player ---

    async def watch_player(self) -> None:
        """Stay connected to whatever radio is playing; it comes and goes."""
        address = paths.ipc_address()
        while True:
            try:
                mpv = await Mpv.connect(address)
            except NotRunning:
                await asyncio.sleep(0.5)
                continue
            self.mpv = mpv
            try:
                # each observed property arrives as property-change, its current value first
                for n, name in enumerate(OBSERVED, 1):
                    await mpv.command("observe_property", n, name)
                while (event := await mpv.next_event()) is not None:
                    if event.get("event") == "property-change":
                        self.props[event["name"]] = event.get("data")
                        self.player_changed(event["name"])
            except MpvError:
                pass
            finally:
                self.mpv = None
                self.props.clear()
                self.position = None
                with contextlib.suppress(Exception):
                    await mpv.close()
                with contextlib.suppress(Exception):  # the app may be shutting down
                    self.player_changed(None)

    async def poll_position(self) -> None:
        # time-pos changes all the time: twice a second is enough, for the lyrics too
        mpv = self.mpv
        if mpv and not mpv.closed:
            with contextlib.suppress(MpvError):
                self.position = await mpv.get("time-pos")
            self.render_now()
            self.sync_lyrics()

    def player_changed(self, name: str | None) -> None:
        if name in (None, LIST_PROPERTY, UPNEXT_PROPERTY, "loop-file", "loop-playlist"):
            self.refresh_lists()
        if name in (None, LIST_PROPERTY):
            self.update_lyrics()
        self.render_now()

    def repeating(self) -> bool:
        return self.props.get("loop-file") not in (None, False, "no")

    def repeating_all(self) -> bool:
        return self.props.get("loop-playlist") not in (None, False, "no")

    def current(self) -> dict | None:
        items = self.props.get(LIST_PROPERTY) or []
        return next((it for it in items if it.get("current")), None)

    def render_now(self) -> None:
        now = self.query_one("#now", Static)
        progress = self.query_one("#progress", ProgressBar)
        if self.mpv is None:
            hint = ("enter on a track starts one, / searches", "dim")
            now.update(Text.assemble(("radio is off", "bold"), "\n", hint))
            progress.display = False
            return
        cur = self.current()
        title = (cur or {}).get("title") or self.props.get("media-title") or ""
        marks = ("↻ " if self.repeating() else "") + (
            "♥ " if cur and cur.get("id") in self.liked_ids else ""
        )
        duration = self.props.get("duration")
        details = [self.props.get(SOURCE_PROPERTY) or RADIO_MIX]
        if marks:
            details.append(marks.strip())
        if self.repeating_all():
            details.append("repeat all")
        details.append(f"{clock(self.position)} / {clock(duration)}")
        volume = self.props.get("volume")
        if volume is not None:
            details.append(f"vol {round(volume)}")
        state = "‖ " if self.props.get("pause") else "▶ "  # ⏸ is missing from many fonts
        now.update(Text.assemble((state + title, "bold"), "\n", (" · ".join(details), "dim")))
        progress.display = bool(duration)
        if duration:
            progress.update(total=duration, progress=self.position or 0)

    # --- the lists ---

    def track_list(self, kind: str) -> TrackList:
        return self.query_one(f"#{kind}-list", TrackList)

    def refresh_lists(self) -> None:
        store = Store()
        liked_ids = self.liked_ids = store.liked_ids()
        # the radio: newest on top; after going back, tracks played "ahead" show above
        radio = list(reversed(self.props.get(LIST_PROPERTY) or []))
        radio_list = self.track_list("radio")
        was_on_current = (
            radio_list.highlighted is not None
            and radio_list.rows
            and radio_list.rows[radio_list.highlighted].get("current")
        )
        lines = control.list_lines(radio, self.repeating(), liked_ids, numbered=False)
        if was_on_current or not radio_list.rows:
            # the cursor follows the playing track, unless the user has moved it
            radio_list.show(radio, lines, True, key=lambda row: bool(row.get("current")))
        else:
            keep = radio_list.rows[radio_list.highlighted or 0].get("entry")
            radio_list.show(radio, lines, keep, key=lambda row: row.get("entry"))
        self.show_upnext(liked_ids)
        for kind, rows in (
            ("recent", store.recent_seeds(10)),
            ("history", store.recent_plays(30)),
            ("liked", list(reversed(store.liked()))),  # newest first
        ):
            heart = kind != "liked"  # every liked track has one: no need to show it there
            lines = [("♥ " if heart and t.id in liked_ids else "") + t.title for t in rows]
            self.track_list(kind).show(rows, lines)

    def show_upnext(self, liked_ids: set[str]) -> None:
        """The queue first, then the radio's own picks under a heading of their own."""
        items = self.props.get(UPNEXT_PROPERTY) or []
        rows: list[Any] = []
        lines: list[Any] = []
        any_liked = any(it.get("id") in liked_ids for it in items)
        # with repeat all it's one loop: the tracks round it aren't the radio's
        radio_picks = not self.repeating_all()
        for n, it in enumerate(items):
            if radio_picks and not it.get("queued"):
                radio_picks = False  # one heading, at the first of them
                if n:
                    rows.append(None)
                    lines.append(Option("", disabled=True))
                rows.append(None)
                lines.append(heading("from the radio mix"))
            mark = ("♥ " if it.get("id") in liked_ids else "  ") if any_liked else ""
            lines.append(mark + it.get("title", "?"))
            rows.append(it)
        if not items:
            off = self.mpv is None
            rows, lines = [None], [heading("radio is off" if off else "nothing up next")]
        lst = self.track_list("upnext")
        keep = lst.rows[lst.highlighted] if lst.highlighted is not None and lst.rows else None
        entry = keep.get("entry") if keep else None
        lst.show(rows, lines, entry, key=lambda row: row.get("entry") if row else None)
        if lst.highlighted is not None and rows[lst.highlighted] is None:
            lst.highlighted = next((i for i, row in enumerate(rows) if row), None)

    def active_list(self) -> TrackList:
        focused = self.focused
        if isinstance(focused, TrackList):
            return focused
        return self.track_list(self.query_one(TabbedContent).active or "radio")

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        self.refresh_lists()
        self.update_lyrics()
        if not isinstance(self.focused, Input):
            self.active_list().focus()

    def on_descendant_focus(self) -> None:
        self.show_keys()

    def show_keys(self) -> None:
        """Two lines of keys: the player's, then the focused list's (or the input's)."""
        focused = self.focused
        if isinstance(focused, Input):
            context = INPUT_KEYS
        else:
            kind = focused.kind if isinstance(focused, TrackList) else "radio"
            context = LIST_KEYS.get(kind, []) + APP_KEYS
        self.query_one("#keys", Static).update(key_line(PLAYER_KEYS) + "\n" + key_line(context))

    @on(OptionList.OptionSelected)
    def row_selected(self, event: OptionList.OptionSelected) -> None:
        if isinstance(event.option_list, TrackList):
            self.act(event.option_list, "enter", event.option_index)

    def action_row(self, key: str) -> None:
        lst = self.focused
        if isinstance(lst, TrackList) and lst.highlighted is not None and lst.rows:
            self.act(lst, key, lst.highlighted)

    def act(self, lst: TrackList, key: str, i: int) -> None:
        """What a key does with a row, as in v0's menus."""
        row = lst.rows[i]
        if row is None:  # a heading
            return
        match lst.kind, key:
            case "upnext", "enter" | "right":
                self.run_worker(self.queue_message(PLAY_MESSAGE, str(row["entry"])))
            case "upnext", "delete":
                self.run_worker(self.queue_message(REMOVE_MESSAGE, str(row["entry"])))
            case "search", "left":
                self.search_go_back()
            case "search", "enter" | "right" if row.kind == music.ALBUMS:
                self.run_worker(self.play_album(row), group="launch", exclusive=True)
            case "search", "enter" | "right" if row.kind == music.ARTISTS:
                self.run_worker(self.show_artist(row), group="search", exclusive=True)
            case "radio", "left":
                # jump back here, keeping the queue
                if not row.get("current"):
                    jump = ("script-message", JUMP_MESSAGE, str(row["entry"]))
                    self.run_worker(self.player_command(*jump))
            case "liked", "right" | "left":
                # the liked tracks in a loop from here down; ← with this one on repeat
                queue = control.liked_queue(lst.rows, i)
                self.launch(row.id, row.title, queue=queue, repeat_one=key == "left")
            case "liked", "delete":
                Store().unlike(row.id)
                self.notify(f"♡ {row.title}")
                self.refresh_lists()
                self.render_now()
            case _, "enter" | "right" if row_id(row):
                self.launch(row_id(row), row_title(row))

    def action_enqueue(self, where: str) -> None:
        """e / E on a row: add it to the queue, or play it next."""
        lst = self.focused
        if not isinstance(lst, TrackList) or lst.highlighted is None or not lst.rows:
            return
        row = lst.rows[lst.highlighted]
        if row is None:
            return
        if lst.kind == "upnext":
            if where == PLAY_NEXT:  # E moves it to the top
                first = next(it for it in lst.rows if it)
                if first is not row:
                    move = (MOVE_MESSAGE, str(row["entry"]), "before", str(first["entry"]))
                    self.run_worker(self.queue_message(*move))
            return
        if isinstance(row, music.Result) and row.kind == music.ARTISTS:
            return
        self.run_worker(self.enqueue(row, where))

    async def enqueue(self, row: Any, where: str) -> None:
        if isinstance(row, music.Result) and row.kind == music.ALBUMS:
            try:
                label, tracks = await asyncio.to_thread(music.album, row.id)
            except Exception:
                self.notify(f"could not open {row.title}", severity="error")
                return
            label = label or row.title
        else:
            label, tracks = row_title(row), [Track(row_id(row), row_title(row))]
        if not tracks or not tracks[0].id:
            return
        try:
            queued = await control.enqueue(tracks, where, source=ALBUM)
        except Failure as e:
            self.notify(str(e), severity="error")
            return
        if queued:
            self.notify(("next: " if where == PLAY_NEXT else "queued: ") + label, timeout=3)
        else:
            self.action_tab("radio")

    async def queue_message(self, *args: str) -> None:
        if not self.mpv:
            self.notify("radio is off")
            return
        with contextlib.suppress(MpvError):
            await control.message(*args)

    def action_move(self, step: int) -> None:
        """shift+↑ / shift+↓ in up next."""
        lst = self.focused
        if not isinstance(lst, TrackList) or lst.kind != "upnext" or lst.highlighted is None:
            return
        row = lst.rows[lst.highlighted]
        if row is None:
            return
        at = lst.highlighted + step
        while 0 <= at < len(lst.rows) and lst.rows[at] is None:
            at += step
        if not 0 <= at < len(lst.rows):
            return
        where = "before" if step < 0 else "after"
        move = (MOVE_MESSAGE, str(row["entry"]), where, str(lst.rows[at]["entry"]))
        self.run_worker(self.queue_message(*move))

    async def action_shuffle(self) -> None:
        if self.mpv:
            self.notify("shuffled up next", timeout=2)
        await self.queue_message(SHUFFLE_MESSAGE)

    async def action_repeat_all(self) -> None:
        if not self.mpv:
            self.notify("radio is off")
            return
        with contextlib.suppress(MpvError):
            await control.set_repeat_all(not self.repeating_all())

    async def action_seek(self, seconds: int) -> None:
        await self.player_command("seek", seconds, "relative")

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab
        self.track_list(tab).focus()

    # --- actions ---

    def launch(self, seed: str | None, label: str, **kwargs: Any) -> None:
        """Start a radio in the background; the UI keeps working meanwhile."""

        async def go() -> None:
            self.notify(f"starting {label}…", timeout=3)
            try:
                await control.start(seed, **kwargs)
            except Failure as e:
                self.notify(str(e), severity="error")
                return
            self.action_tab("radio")

        # a newer choice replaces one still starting
        self.run_worker(go(), group="launch", exclusive=True)

    async def player_command(self, *args: Any) -> None:
        if not self.mpv:
            self.notify("radio is off")
            return
        with contextlib.suppress(MpvError):
            await self.mpv.command(*args)

    async def action_pause(self) -> None:
        await self.player_command("cycle", "pause")

    async def action_next(self) -> None:
        await self.player_command("playlist-next", "force")

    async def action_prev(self) -> None:
        await self.player_command("playlist-prev")

    async def action_volume(self, delta: int) -> None:
        volume = await control.change_volume(delta)
        if self.mpv is None:
            self.notify(f"volume {volume} for the next radio")

    async def action_repeat(self) -> None:
        await control.set_repeat(not self.repeating())
        if self.mpv is None:
            self.notify("repeat is " + ("on" if Store().settings().repeat else "off"))

    def action_like(self) -> None:
        cur = self.current()
        if not cur or not cur.get("id"):
            self.notify("nothing is playing")
            return
        store = Store()
        track = Track(cur["id"], cur.get("title") or cur["id"])
        if track.id in store.liked_ids():
            store.unlike(track.id)
            self.notify(f"♡ {track.title}")
        else:
            store.like(track)
            self.notify(f"♥ {track.title}")
        self.refresh_lists()
        self.render_now()

    async def action_stop(self) -> None:
        await control.stop()

    def action_search(self) -> None:
        self.ask_for("search")

    def action_ai(self) -> None:
        self.ask_for("ai")

    def ask_for(self, mode: str) -> None:
        """The input at the bottom takes a search, or a mood for the AI radio."""
        field = self.query_one("#search", Input)
        field.placeholder = AI_HINT if mode == "ai" else SEARCH_HINT
        field.focus()

    @on(Input.Blurred, "#search")
    def input_left(self, event: Input.Blurred) -> None:
        event.input.placeholder = SEARCH_HINT

    def action_back(self) -> None:
        if isinstance(self.focused, Input):
            self.active_list().focus()
        elif self.focused is self.track_list("search") and self.search_back:
            self.search_go_back()
        else:
            self.exit()

    # --- lyrics ---

    def update_lyrics(self) -> None:
        """Show the playing track's lyrics while the lyrics tab is open."""
        if self.query_one(TabbedContent).active != "lyrics":
            return
        cur = self.current() or {}
        video_id = cur.get("id") or None
        if video_id == self.lyrics_id:
            return
        self.lyrics_id = video_id
        view = self.track_list("lyrics")
        if not video_id:
            view.show([None], ["radio is off" if self.mpv is None else "nothing is playing"])
        elif video_id in self.lyrics_cache:
            self.show_lyrics(video_id)
        else:
            view.show([None], ["loading lyrics…"])
            self.run_worker(self.load_lyrics(video_id), group="lyrics", exclusive=True)

    async def load_lyrics(self, video_id: str) -> None:
        try:
            self.lyrics_cache[video_id] = await asyncio.to_thread(music.lyrics, video_id)
        except Exception:
            self.lyrics_cache[video_id] = None
        if self.lyrics_id == video_id:
            self.show_lyrics(video_id)

    def show_lyrics(self, video_id: str) -> None:
        lyrics = self.lyrics_cache.get(video_id)
        title = Text((self.current() or {}).get("title") or "", style="bold")
        if not lyrics:
            lines: list[Any] = [title, "", Text("no lyrics for this track", style="dim")]
        else:
            lines = [title, "", *(text or "♪" for _, text in lyrics.lines)]
            if lyrics.source:
                lines += ["", Text(lyrics.source, style="dim")]
        view = self.track_list("lyrics")
        view.show([None] * len(lines), lines)
        view.highlighted = LYRICS_TOP if lyrics else None
        self.sync_lyrics()

    def sync_lyrics(self) -> None:
        """Keep the line being sung highlighted (and in view)."""
        lyrics = self.lyrics_cache.get(self.lyrics_id or "")
        if not lyrics or not lyrics.timed or self.position is None:
            return
        if self.query_one(TabbedContent).active != "lyrics":
            return
        at = self.position + 0.3  # a line shows a moment before it's sung
        sung = [i for i, (start, _) in enumerate(lyrics.lines) if start is not None and start <= at]
        line = LYRICS_TOP + (sung[-1] if sung else 0)
        view = self.track_list("lyrics")
        if view.highlighted != line:
            view.highlighted = line
            # the sung line stays in the middle, like karaoke
            view.scroll_to(y=max(0, line - view.size.height // 2), animate=False)

    # --- search ---

    def show_search(self, rows: list[Any], options: list[Any]) -> None:
        lst = self.track_list("search")
        lst.rows = rows
        lst.set_options(options)
        lst.highlighted = next((i for i, row in enumerate(rows) if row is not None), None)

    @on(Input.Submitted, "#search")
    def search(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        ai_mode = event.input.placeholder == AI_HINT
        event.input.clear()
        if ai_mode:
            self.active_list().focus()
            self.run_worker(self.ai_radio(text), group="launch", exclusive=True)
        else:
            self.action_tab("search")
            self.run_worker(self.find(text), group="search", exclusive=True)

    async def ai_radio(self, mood: str) -> None:
        self.notify(f"asking the ai for «{mood}»…", timeout=5)
        try:
            tracks = await control.ai_radio(mood)
        except Failure as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        self.notify(f"ai radio: {len(tracks)} songs, then the mix", timeout=5)
        self.action_tab("radio")

    async def find(self, query: str) -> None:
        self.search_back.clear()
        self.show_search([None], [heading(f"searching «{query}»…")])
        try:
            found = await asyncio.to_thread(music.search, query)
        except Exception:
            self.show_search([None], [heading(f"search failed: «{query}»")])
            return
        rows: list[Any] = []
        options: list[Any] = []
        for title, results in (
            ("Songs", found.songs),
            ("Albums", found.albums),
            ("Artists", found.artists),
        ):
            if results:
                if rows:
                    rows.append(None)
                    options.append(Option("", disabled=True))
                rows.append(None)
                options.append(heading(title))
                rows += results
                options += [result_line(r) for r in results]
        if not rows:
            rows, options = [None], [heading(f"nothing found: «{query}»")]
        self.show_search(rows, options)

    async def show_artist(self, artist: music.Result) -> None:
        lst = self.track_list("search")
        before = (lst.rows, [lst.get_option_at_index(i) for i in range(lst.option_count)])
        try:
            name, songs = await asyncio.to_thread(music.artist_songs, artist.id)
        except Exception:
            self.notify(f"could not open {artist.title}", severity="error")
            return
        if not songs:
            self.notify(f"no songs of {artist.title}")
            return
        self.search_back.append(before)
        header = heading(f"{name or artist.title}: top songs  (← back)")
        self.show_search([None, *songs], [header, *map(result_line, songs)])

    def search_go_back(self) -> None:
        if self.search_back:
            self.show_search(*self.search_back.pop())

    async def play_album(self, album: music.Result) -> None:
        """The album in order, then a radio from its last track."""
        self.notify(f"starting {album.title}…", timeout=3)
        try:
            title, tracks = await asyncio.to_thread(music.album, album.id)
        except Exception:
            self.notify(f"could not open {album.title}", severity="error")
            return
        if not tracks:
            self.notify(f"nothing playable on {album.title}")
            return
        try:
            await control.start(tracks[0].id, queue=tracks, then_radio=True, source=ALBUM)
        except Failure as e:
            self.notify(str(e), severity="error")
            return
        self.action_tab("radio")


def run() -> None:
    MusicApp().run()
