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
from textual.widgets import Footer, Input, OptionList, ProgressBar, Static, TabbedContent, TabPane

from . import control, paths
from .control import Failure
from .ipc import Mpv, MpvError, NotRunning
from .models import Track
from .radio import JUMP_MESSAGE, LIST_PROPERTY, RADIO_MIX, SOURCE_PROPERTY
from .store import Store

OBSERVED = ("media-title", "pause", "volume", "loop-file", "duration")
OBSERVED += (LIST_PROPERTY, SOURCE_PROPERTY)

# tab id -> title; keys 1-4 switch between them
TABS = {"radio": "Radio", "recent": "Recent", "history": "History", "liked": "Liked"}


def clock(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    s = int(seconds)
    return f"{s // 3600}:{s // 60 % 60:02}:{s % 60:02}" if s >= 3600 else f"{s // 60}:{s % 60:02}"


class TrackList(OptionList):
    """Tracks of one tab. `rows` are the radio's list items (dicts) or Tracks."""

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


def row_id(row: Any) -> str:
    return (row.get("id") or "") if isinstance(row, dict) else row.id


def row_title(row: Any) -> str:
    return row.get("title", "?") if isinstance(row, dict) else row.title


class MusicApp(App):
    TITLE = "musictty"
    CSS = """
    #now { padding: 1 2 0 2; height: auto; }
    #progress { padding: 0 2 1 2; }
    #progress Bar { width: 1fr; }
    TabbedContent { height: 1fr; }
    TrackList { height: 1fr; border: none; }
    #search { margin: 0 1; }
    """

    BINDINGS = [
        Binding("space", "pause", "pause"),
        Binding("n", "next", "next"),
        Binding("p", "prev", "prev"),
        Binding("plus,equals_sign", "volume(5)", "vol+", key_display="+"),
        Binding("minus", "volume(-5)", "vol-", key_display="-"),
        Binding("l", "like", "like"),
        Binding("r", "repeat", "repeat"),
        Binding("s", "stop", "stop"),
        Binding("slash", "search", "search", key_display="/"),
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

    def compose(self) -> ComposeResult:
        yield Static(id="now")
        yield ProgressBar(id="progress", show_eta=False, show_percentage=False)
        with TabbedContent(initial="radio"):
            for n, (tab, title) in enumerate(TABS.items(), 1):
                with TabPane(f"{n} {title}", id=tab):
                    yield TrackList(tab)
        yield Input(placeholder="/ search YouTube Music, enter starts a radio", id="search")
        yield Footer()

    def on_mount(self) -> None:
        self.render_now()
        self.refresh_lists()
        self.track_list("radio").focus()
        self.run_worker(self.watch_player(), group="player")
        self.set_interval(1, self.poll_position)

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
        # time-pos changes all the time: once a second is enough
        mpv = self.mpv
        if mpv and not mpv.closed:
            with contextlib.suppress(MpvError):
                self.position = await mpv.get("time-pos")
            self.render_now()

    def player_changed(self, name: str | None) -> None:
        if name in (None, LIST_PROPERTY, "loop-file"):
            self.refresh_lists()
        self.render_now()

    def repeating(self) -> bool:
        return self.props.get("loop-file") not in (None, False, "no")

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
        details.append(f"{clock(self.position)} / {clock(duration)}")
        volume = self.props.get("volume")
        if volume is not None:
            details.append(f"vol {round(volume)}")
        state = "⏸ " if self.props.get("pause") else "▶ "
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
        for kind, rows in (
            ("recent", store.recent_seeds(10)),
            ("history", store.recent_plays(30)),
            ("liked", list(reversed(store.liked()))),  # newest first
        ):
            heart = kind != "liked"  # every liked track has one: no need to show it there
            lines = [("♥ " if heart and t.id in liked_ids else "") + t.title for t in rows]
            self.track_list(kind).show(rows, lines)

    def active_list(self) -> TrackList:
        focused = self.focused
        if isinstance(focused, TrackList):
            return focused
        return self.track_list(self.query_one(TabbedContent).active or "radio")

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        self.refresh_lists()
        if not isinstance(self.focused, Input):
            self.active_list().focus()

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
        match lst.kind, key:
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
        self.query_one("#search", Input).focus()

    def action_back(self) -> None:
        if isinstance(self.focused, Input):
            self.active_list().focus()
        else:
            self.exit()

    @on(Input.Submitted, "#search")
    def search(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if query:
            event.input.clear()
            self.active_list().focus()
            self.launch(None, f"«{query}»", query=query)


def run() -> None:
    MusicApp().run()
