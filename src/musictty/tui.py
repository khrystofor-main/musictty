"""The terminal UI: `musictty` with no arguments.

Just another client of mpv's IPC, like the CLI: it watches the player and the track list the
radio publishes, and starts radios through control.start. Quitting it leaves the music playing.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from rich.cells import cell_len
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, ProgressBar, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from . import control, music, paths, player
from .control import Failure
from .dialogs import Ask, Choose, Confirm, Pick
from .ipc import Mpv, MpvError, NotRunning
from .models import Track
from .radio import (
    ADD_TO_QUEUE,
    ALBUM,
    ARTIST_RADIO,
    JUMP_MESSAGE,
    LIST_PROPERTY,
    MOVE_MESSAGE,
    PLAY_MESSAGE,
    PLAY_NEXT,
    PLAYLIST,
    RADIO_MIX,
    REMOVE_MESSAGE,
    SHUFFLE_MESSAGE,
    SLEEP_END,
    SLEEP_MESSAGE,
    SLEEP_PROPERTY,
    SOURCE_PROPERTY,
    UPNEXT_PROPERTY,
)
from .store import Playlist, PlaylistError, Store

OBSERVED = ("media-title", "pause", "volume", "loop-file", "loop-playlist", "duration")
OBSERVED += (LIST_PROPERTY, SOURCE_PROPERTY, UPNEXT_PROPERTY, SLEEP_PROPERTY)
# the sleep timer's choices: seconds, or the end of the track
SLEEP_CHOICES = [
    ("off", "0"),
    ("in 15 minutes", "900"),
    ("in 30 minutes", "1800"),
    ("in 45 minutes", "2700"),
    ("in 1 hour", "3600"),
    ("in 1.5 hours", "5400"),
    ("after this track", SLEEP_END),
]
QUALITY_LABELS = {
    "low": "low · about 50–70 kbit/s, saves traffic",
    "normal": "normal · about 130–160 kbit/s",
    "high": "high · the best stream there is",
}

# tab id -> title; keys 1-9 switch between them
TABS = {
    "radio": "Radio",
    "recent": "Recent",
    "history": "History",
    "liked": "Liked",
    "search": "Search",
    "lyrics": "Lyrics",
    "upnext": "Up next",
    "playlists": "Playlists",
    "explore": "Home",
}
NEW_PLAYLIST = "+ new playlist"  # the first row of the playlists tab
SEEK_STEP = 10  # seconds

LYRICS_TOP = 2  # the track's title and a blank line above the lyrics


SEARCH_HINT = "/ search YouTube Music: songs, albums, artists, playlists"
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
        # search and explore: the page shown (None: the results), and what it replaced, for ←
        self.page: music.Page | None = None
        self.back: list[tuple[list[Any], list[Any], music.Page | None]] = []

    def replace(self, rows: list[Any], options: list[Any]) -> None:
        """New rows with the cursor on the first that isn't a heading."""
        self.rows = rows
        self.set_options(options)
        self.highlighted = next((i for i, row in enumerate(rows) if row is not None), None)

    def push(self, page: music.Page, rows: list[Any], options: list[Any]) -> None:
        options_now = [self.get_option_at_index(i) for i in range(self.option_count)]
        self.back.append((self.rows, options_now, self.page))
        self.page = page
        self.replace(rows, options)

    def pop(self) -> bool:
        if not self.back:
            return False
        rows, options, self.page = self.back.pop()
        self.replace(rows, options)
        return True

    def reset(self, rows: list[Any], options: list[Any]) -> None:
        self.back.clear()
        self.page = None
        self.replace(rows, options)

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


def page_line(result: music.Result, page: music.Page, n: int | None) -> Text:
    """A row of an artist's or an album's page."""
    if result.kind == music.RADIO:
        return Text.assemble("▶ ", result.title)
    if result.kind in (music.MORE_SONGS, music.MORE_ALBUMS, music.MORE_RESULTS):
        return Text.assemble((result.title, "italic"), ("  →", "dim"))
    if n is None:
        return result_line(result)
    # an album's tracks: numbered, without the album's artist in front of each
    title = result.title.removeprefix(f"{page.artist} — ") if page.artist else result.title
    return Text.assemble((f"{n:2}. ", "dim"), title, (f"  {result.detail}", "dim"))


def page_rows(page: music.Page, back: bool) -> tuple[list[Any], list[Any]]:
    """A page's rows (Results, None for the rest) and their lines."""
    rows: list[Any] = [None]
    lines: list[Any] = [heading(f"{page.title}  (← back)" if back else page.title)]
    if page.detail:
        rows.append(None)
        lines.append(Option(Text(page.detail, style="dim"), disabled=True))
    for section in page.sections:
        rows.append(None)
        lines.append(Option("", disabled=True))
        if section.title:
            rows.append(None)
            lines.append(heading(section.title))
        for n, result in enumerate(section.results, 1):
            rows.append(result)
            lines.append(page_line(result, page, n if section.numbered else None))
    return rows, lines


# the lists that show pages: ← goes back, → opens
BROWSERS = {"search", "explore"}
# rows of the search and explore tabs that open a page
PAGES = {music.ARTISTS, music.ALBUMS, music.PLAYLISTS, music.MORE_SONGS, music.MORE_ALBUMS}
PAGES |= {music.MOODS, music.MORE_RESULTS}
# rows that stand for several tracks: they play in order, then a radio
COLLECTIONS = {music.ALBUMS: ("album", ALBUM), music.PLAYLISTS: ("playlist", PLAYLIST)}


def row_id(row: Any) -> str:
    return (row.get("id") or "") if isinstance(row, dict) else row.id


def row_key(row: Any) -> Any:
    """What keeps the cursor on a row of the playlists tab when it's redrawn."""
    if isinstance(row, Playlist):
        return ("playlist", row.name)
    return row.id if isinstance(row, Track) else row


def playlist_line(playlist: Playlist) -> Text:
    n = len(playlist.tracks)
    return Text.assemble(playlist.name, (f"  {n} song{'' if n == 1 else 's'}", "dim"))


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
QUEUE_KEYS = [("e/E", "queue/next"), ("S", "save"), ("g", "go to")]
LIST_KEYS = {
    "radio": [("enter", "radio"), ("←", "jump back"), *QUEUE_KEYS],
    "recent": [("enter", "radio"), *QUEUE_KEYS],
    "history": [("enter", "radio"), *QUEUE_KEYS],
    "liked": [
        ("enter", "radio"),
        ("→/←", "loop"),
        ("del", "unlike"),
        *QUEUE_KEYS,
    ],
    "search": [("enter", "play"), ("→", "open"), ("←", "back"), *QUEUE_KEYS],
    "explore": [("enter", "play"), ("→", "open"), ("←", "back"), *QUEUE_KEYS],
    "lyrics": [],
    "upnext": [
        ("enter", "play"),
        ("E", "top"),
        ("shift+↑/↓", "move"),
        ("del", "remove"),
        ("S", "save"),
    ],
    "playlists": [("enter", "play"), ("→", "open"), ("del", "delete")],
    "playlist": [
        ("enter", "play"),
        ("←", "back"),
        ("shift+↑/↓", "move"),
        ("del", "remove"),
        ("e/E", "queue/next"),
    ],
}
APP_KEYS = [("/", "search"), ("a", "ai"), ("z", "sleep"), ("Q", "quality"), ("q", "quit")]
INPUT_KEYS = [("enter", "go"), ("esc", "back")]
SEARCH_KEYS = [("enter", "search"), ("↓", "suggestions"), ("esc", "back")]
SUGGESTION_KEYS = [("enter", "search this"), ("esc", "back to typing")]
SUGGEST_AFTER = 0.25  # seconds of no typing before suggestions are asked for


def key_lines(keys: list[tuple[str, str]], width: int) -> list[str]:
    """Keys and what they do, as many to a line as fit: a line breaks between them only."""
    lines: list[list[str]] = [[]]
    used = 0
    for key, desc in keys:
        size = cell_len(f"{key} {desc}")
        if lines[-1] and used + 2 + size > width:
            lines.append([])
            used = 0
        used += size + (2 if lines[-1] else 0)
        lines[-1].append(f"[b $footer-key-foreground]{key}[/] {desc}")
    return ["  ".join(line) for line in lines]


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
    #suggestions { height: auto; max-height: 8; margin: 0 2; border: none; display: none; }
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
        Binding("S,shift+s", "save", "to playlist", key_display="S"),
        Binding("z", "sleep", "sleep timer"),
        Binding("g", "goto", "go to artist or album"),
        Binding("Q,shift+q", "quality", "audio quality", key_display="Q"),
        Binding("shift+up", "move(-1)", "up", show=False),
        Binding("shift+down", "move(1)", "down", show=False),
        Binding("slash", "search", "search", key_display="/"),
        Binding("a", "ai", "ai radio"),
        Binding("q", "quit", "quit"),
        Binding("escape", "back", show=False),
        Binding("down", "suggestions", show=False),
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
        self.explore_loaded = False  # the explore tab loads when it's first opened
        self.suggestion_cache: dict[str, list[str]] = {}
        # the playlists tab: the playlist open in it, None for the list of them
        self.playlist_open: str | None = None
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
        yield OptionList(id="suggestions")
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
        if sleep := control.sleep_text(self.props.get(SLEEP_PROPERTY), time.time()):
            details.append(sleep)
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
        self.show_playlists(store, liked_ids)
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

    def show_playlists(self, store: Store, liked_ids: set[str]) -> None:
        """Your playlists, or the tracks of the one open."""
        lst = self.track_list("playlists")
        keep = lst.rows[lst.highlighted] if lst.highlighted is not None and lst.rows else None
        playlists = store.playlists()
        playlist = next((p for p in playlists if p.name == self.playlist_open), None)
        if playlist is None:
            self.playlist_open = None
            rows: list[Any] = [NEW_PLAYLIST, *playlists]
            lines: list[Any] = [Text(NEW_PLAYLIST, style="italic")]
            lines += [playlist_line(p) for p in playlists]
            lst.show(rows, lines, row_key(keep), key=row_key)
            return
        rows = [None, *playlist.tracks]
        lines = [heading(f"{playlist.name}  (← back)")]
        lines += [("♥ " if t.id in liked_ids else "") + t.title for t in playlist.tracks]
        if not playlist.tracks:
            rows.append(None)
            lines.append(Text("empty: S on a track in any list adds it here", style="dim"))
        lst.show(rows, lines, row_key(keep), key=row_key)
        if lst.highlighted is not None and rows[lst.highlighted] is None:
            lst.highlighted = next((i for i, row in enumerate(rows) if row), None)

    def open_playlist(self, name: str | None) -> None:
        self.playlist_open = name
        lst = self.track_list("playlists")
        lst.rows = []  # a fresh cursor
        lst.highlighted = None
        self.refresh_lists()
        self.show_keys()

    def active_list(self) -> TrackList:
        focused = self.focused
        if isinstance(focused, TrackList):
            return focused
        return self.track_list(self.query_one(TabbedContent).active or "radio")

    @on(TabbedContent.TabActivated)
    def tab_activated(self) -> None:
        self.refresh_lists()
        self.update_lyrics()
        if self.query_one(TabbedContent).active == "explore" and not self.explore_loaded:
            self.explore_loaded = True
            self.run_worker(self.load_explore(), group="explore", exclusive=True)
        if not isinstance(self.focused, Input):
            self.active_list().focus()

    def on_descendant_focus(self) -> None:
        self.show_keys()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # a dialog is open: its keys only, nothing happens behind it
        return not isinstance(self.screen, ModalScreen)

    def show_keys(self) -> None:
        """Two lines of keys: the player's, then the focused list's (or the input's)."""
        focused = self.focused
        if isinstance(focused, Input):
            context = INPUT_KEYS if focused.placeholder == AI_HINT else SEARCH_KEYS
        elif focused is self.query_one("#suggestions"):
            context = SUGGESTION_KEYS
        else:
            kind = focused.kind if isinstance(focused, TrackList) else "radio"
            if kind == "playlists" and self.playlist_open is not None:
                kind = "playlist"
            context = LIST_KEYS.get(kind, []) + APP_KEYS
        bar = self.query_one("#keys", Static)
        width = max(20, (bar.size.width or self.size.width) - 2)
        bar.update("\n".join(key_lines(PLAYER_KEYS, width) + key_lines(context, width)))

    def on_resize(self) -> None:
        self.call_after_refresh(self.show_keys)

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
            case "playlists", "enter" if row == NEW_PLAYLIST:
                self.push_screen(Ask("name of the new playlist"), self.create_playlist)
            case "playlists", "enter" if isinstance(row, Playlist):
                self.run_worker(self.play_collection(row), group="launch", exclusive=True)
            case "playlists", "right" if isinstance(row, Playlist):
                self.open_playlist(row.name)
            case "playlists", "delete" if isinstance(row, Playlist):
                self.push_screen(
                    Confirm(f"delete the playlist «{row.name}»?"),
                    lambda yes: yes and self.delete_playlist(row.name),
                )
            case "playlists", "left" if self.playlist_open is not None:
                self.open_playlist(None)
            case "playlists", "enter" | "right" if isinstance(row, Track):
                tracks = [r for r in lst.rows[i:] if isinstance(r, Track)]
                self.launch(row.id, row.title, queue=tracks, then_radio=True, source=PLAYLIST)
            case "playlists", "delete" if isinstance(row, Track) and self.playlist_open:
                Store().remove_from_playlist(self.playlist_open, row.id)
                self.refresh_lists()
            case "playlists", _:
                pass  # "+ new playlist" has nothing else
            case "upnext", "enter" | "right":
                self.run_worker(self.queue_message(PLAY_MESSAGE, str(row["entry"])))
            case "upnext", "delete":
                self.run_worker(self.queue_message(REMOVE_MESSAGE, str(row["entry"])))
            case kind, "left" if kind in BROWSERS:
                lst.pop()
            case kind, "enter" if kind in BROWSERS and row.kind in COLLECTIONS:
                self.run_worker(self.play_collection(row), group="launch", exclusive=True)
            case kind, "enter" | "right" if kind in BROWSERS and row.kind in PAGES:
                self.run_worker(self.open_page(lst, row), group=kind, exclusive=True)
            case kind, "enter" | "right" if kind in BROWSERS and row.kind == music.RADIO:
                self.run_worker(self.artist_radio(row), group="launch", exclusive=True)
            case kind, "enter" | "right" if (
                kind in BROWSERS and row.kind == music.SONGS and lst.page and lst.page.plays_as
            ):
                self.play_from(lst, i, lst.page.plays_as)
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
        if row == NEW_PLAYLIST:
            return
        self.run_worker(self.enqueue(row, where))

    async def row_tracks(self, row: Any) -> tuple[str, list[Track], str] | None:
        """The tracks behind a row: the track itself, or an album's or a playlist's. Returns
        (label, tracks, the source they play as); None if there are none."""
        if isinstance(row, music.Result) and row.kind in COLLECTIONS:
            fetch, source = COLLECTIONS[row.kind]
            try:
                label, tracks = await asyncio.to_thread(getattr(music, fetch), row.id)
            except Exception:
                self.notify(f"could not open {row.title}", severity="error")
                return None
            found = (label or row.title, tracks, source)
        elif isinstance(row, Playlist):
            found = (row.name, row.tracks, PLAYLIST)
        elif isinstance(row, music.Result) and row.kind != music.SONGS:
            return None  # an artist, a radio, a "more" link
        elif row_id(row):
            found = (row_title(row), [Track(row_id(row), row_title(row))], RADIO_MIX)
        else:
            return None
        if not found[1]:
            self.notify(f"nothing playable in {found[0]}")
            return None
        return found

    async def enqueue(self, row: Any, where: str) -> None:
        found = await self.row_tracks(row)
        if not found:
            return
        label, tracks, source = found
        try:
            queued = await control.enqueue(tracks, where, source=source)
        except Failure as e:
            self.notify(str(e), severity="error")
            return
        if queued:
            self.notify(("next: " if where == PLAY_NEXT else "queued: ") + label, timeout=3)
        else:
            self.action_tab("radio")

    async def play_collection(self, row: Any) -> None:
        """An album or a playlist: in order, then a radio from its last track."""
        name = row.name if isinstance(row, Playlist) else row_title(row)
        self.notify(f"starting {name}…", timeout=3)
        found = await self.row_tracks(row)
        if not found:
            return
        _, tracks, source = found
        try:
            await control.start(tracks[0].id, queue=tracks, then_radio=True, source=source)
        except Failure as e:
            self.notify(str(e), severity="error")
            return
        self.action_tab("radio")

    # --- go to an artist or an album ---

    def action_goto(self) -> None:
        """g on a track (or with none, the one playing): its artist's or album's page."""
        lst = self.focused
        row: Any = None
        if isinstance(lst, TrackList) and lst.highlighted is not None and lst.rows:
            row = lst.rows[lst.highlighted]
            if isinstance(lst, TrackList) and lst.kind == "lyrics":
                row = None
        if isinstance(row, music.Result) and row.kind in PAGES:
            self.run_worker(self.open_in_search(row))  # an album, an artist: itself
            return
        if isinstance(row, music.Result) and row.kind != music.SONGS or row == NEW_PLAYLIST:
            return
        row = row or self.current()
        if isinstance(row, Playlist) or not row or not row_id(row):
            return
        self.run_worker(self.goto(row_id(row), row_title(row)), group="goto", exclusive=True)

    async def goto(self, video_id: str, title: str) -> None:
        try:
            links = await asyncio.to_thread(music.track_links, video_id)
        except Exception:
            links = []
        if not links:
            self.notify(f"no artist or album for {title}")
            return
        if len(links) == 1:
            await self.open_in_search(links[0])
            return
        labels = [f"{'album' if r.kind == music.ALBUMS else 'artist'} · {r.title}" for r in links]

        def chosen(index: int | None) -> None:
            if index is not None:
                self.run_worker(self.open_in_search(links[index]))

        self.push_screen(Choose(f"go to: {title}", labels), chosen)

    async def open_in_search(self, row: music.Result) -> None:
        """A page opens in the search tab, on top of what it shows."""
        self.action_tab("search")
        await self.open_page(self.track_list("search"), row)

    # --- playlists ---

    def action_save(self) -> None:
        """S: save the row's track (an album's, a playlist's tracks) to one of your playlists;
        outside the lists, the track that's playing."""
        lst = self.focused
        row: Any = None
        if isinstance(lst, TrackList) and lst.highlighted is not None and lst.rows:
            row = lst.rows[lst.highlighted]
        if (
            row is None
            or row == NEW_PLAYLIST
            or (isinstance(lst, TrackList) and lst.kind == "lyrics")
        ):
            row = self.current()
        if not row:
            return
        self.run_worker(self.save(row))

    async def save(self, row: Any) -> None:
        found = await self.row_tracks(row)
        if not found:
            return
        label, tracks, _ = found
        names = [p.name for p in Store().playlists()]

        def picked(choice: tuple[str, bool] | None) -> None:
            if not choice:
                return
            name, new = choice
            store = Store()
            try:
                if new:
                    store.create_playlist(name)
                added = store.add_to_playlist(name, tracks)
            except PlaylistError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"{label} → {name}" if added else f"already in {name}", timeout=3)
            self.refresh_lists()

        self.push_screen(Pick(f"save {label} to", names), picked)

    def create_playlist(self, name: str | None) -> None:
        if not name:
            return
        try:
            Store().create_playlist(name)
        except PlaylistError as e:
            self.notify(str(e), severity="error")
            return
        self.refresh_lists()

    def delete_playlist(self, name: str) -> None:
        with contextlib.suppress(PlaylistError):
            Store().delete_playlist(name)
        self.notify(f"deleted {name}", timeout=3)
        self.refresh_lists()

    async def queue_message(self, *args: str) -> None:
        if not self.mpv:
            self.notify("radio is off")
            return
        with contextlib.suppress(MpvError):
            await control.message(*args)

    def action_move(self, step: int) -> None:
        """shift+↑ / shift+↓ in up next and in a playlist."""
        lst = self.focused
        if not isinstance(lst, TrackList) or lst.highlighted is None or not lst.rows:
            return
        if lst.kind == "playlists" and self.playlist_open is not None:
            row = lst.rows[lst.highlighted]
            if isinstance(row, Track):
                Store().move_in_playlist(self.playlist_open, row.id, step)
                self.refresh_lists()
            return
        if lst.kind != "upnext":
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

    def action_sleep(self) -> None:
        """z: the sleep timer, kept by the radio itself."""
        if not self.mpv:
            self.notify("radio is off")
            return
        # the choice marked: "off" when there's no timer, "after this track"; none for a time
        value = self.props.get(SLEEP_PROPERTY)
        timer = control.sleep_text(value, time.time())
        current = len(SLEEP_CHOICES) - 1 if value == SLEEP_END else (None if timer else 0)

        def chosen(index: int | None) -> None:
            if index is not None:
                self.run_worker(self.queue_message(SLEEP_MESSAGE, SLEEP_CHOICES[index][1]))

        labels = [label for label, _ in SLEEP_CHOICES]
        self.push_screen(Choose("sleep timer: stop the radio", labels, current), chosen)

    def action_quality(self) -> None:
        """Q: the audio quality, for the tracks to come and the next radios."""
        qualities = list(QUALITY_LABELS)
        current = qualities.index(Store().settings().quality)

        def chosen(index: int | None) -> None:
            if index is None:
                return
            self.run_worker(control.set_quality(qualities[index]))
            self.notify(f"audio quality: {qualities[index]}", timeout=3)

        self.push_screen(Choose("audio quality", list(QUALITY_LABELS.values()), current), chosen)

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
        self.show_keys()

    @on(Input.Blurred, "#search")
    def input_left(self, event: Input.Blurred) -> None:
        suggestions = self.query_one("#suggestions", OptionList)

        def settled() -> None:
            # into the suggestions, the search goes on; anywhere else, it's left
            if self.focused is not suggestions:
                event.input.placeholder = SEARCH_HINT
                suggestions.display = False

        self.call_after_refresh(settled)

    # --- search suggestions ---

    @on(Input.Changed, "#search")
    def typed(self, event: Input.Changed) -> None:
        text = event.value.strip()
        if not text or event.input.placeholder == AI_HINT:
            self.query_one("#suggestions").display = False
            self.workers.cancel_group(self, "suggest")
            return
        self.run_worker(self.suggest(text), group="suggest", exclusive=True)

    async def suggest(self, text: str) -> None:
        if text not in self.suggestion_cache:
            await asyncio.sleep(SUGGEST_AFTER)  # typing on: this worker is replaced
            try:
                self.suggestion_cache[text] = await asyncio.to_thread(music.suggestions, text)
            except Exception:
                return  # no suggestions: the search itself still works
        field = self.query_one("#search", Input)
        if field.value.strip() != text or self.focused is not field:
            return
        found = self.suggestion_cache[text]
        suggestions = self.query_one("#suggestions", OptionList)
        suggestions.set_options(found)
        suggestions.display = bool(found)

    def action_suggestions(self) -> None:
        """↓ in the search: into the suggestions."""
        suggestions = self.query_one("#suggestions", OptionList)
        if isinstance(self.focused, Input) and suggestions.display and suggestions.option_count:
            suggestions.focus()
            suggestions.highlighted = 0

    @on(OptionList.OptionSelected, "#suggestions")
    def suggestion_picked(self, event: OptionList.OptionSelected) -> None:
        text = str(event.option.prompt)
        field = self.query_one("#search", Input)
        field.clear()
        event.option_list.display = False
        self.action_tab("search")
        self.run_worker(self.find(text), group="search", exclusive=True)

    def action_back(self) -> None:
        suggestions = self.query_one("#suggestions", OptionList)
        if self.focused is suggestions:
            self.query_one("#search", Input).focus()  # back to typing
        elif isinstance(self.focused, Input):
            suggestions.display = False
            self.active_list().focus()
        elif isinstance(self.focused, TrackList) and self.focused.pop():
            pass  # back from a page
        elif self.focused is self.track_list("playlists") and self.playlist_open is not None:
            self.open_playlist(None)
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
        self.track_list("search").reset(rows, options)

    @on(Input.Submitted, "#search")
    def search(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        ai_mode = event.input.placeholder == AI_HINT
        event.input.clear()
        self.query_one("#suggestions").display = False
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
        self.show_search([None], [heading(f"searching «{query}»…")])
        try:
            found = await asyncio.to_thread(music.search, query)
        except Exception:
            self.show_search([None], [heading(f"search failed: «{query}»")])
            return
        rows: list[Any] = []
        options: list[Any] = []
        for kind, results in (
            ("songs", found.songs),
            ("albums", found.albums),
            ("artists", found.artists),
            ("playlists", found.playlists),
        ):
            if results:
                if rows:
                    rows.append(None)
                    options.append(Option("", disabled=True))
                rows.append(None)
                options.append(heading(kind.capitalize()))
                rows += results
                options += [result_line(r) for r in results]
                if len(results) >= music.SEARCH_LIMIT:  # there may be more
                    more = music.Result(music.MORE_RESULTS, query, f"all {kind}", params=kind)
                    rows.append(more)
                    options.append(page_line(more, music.Page(""), None))
        if not rows:
            rows, options = [None], [heading(f"nothing found: «{query}»")]
        self.show_search(rows, options)

    async def load_explore(self) -> None:
        lst = self.track_list("explore")
        lst.reset([None], [heading("loading home…")])
        try:
            page = await asyncio.to_thread(music.explore)
        except Exception:
            page = music.Page("Explore")
        if not page.sections:
            self.explore_loaded = False  # the next visit tries again
            lst.reset([None], [heading("could not load home: open the tab again to retry")])
            return
        lst.reset(*page_rows(page, back=False))

    async def open_page(self, lst: TrackList, row: music.Result) -> None:
        """An artist's, an album's or a playlist's page, all of an artist's songs or albums,
        the playlists of a mood."""
        name = lst.page.title if lst.page else ""
        loaders: dict[str, Any] = {
            music.ARTISTS: lambda: music.artist(row.id),
            music.ALBUMS: lambda: music.album_page(row.id),
            music.PLAYLISTS: lambda: music.playlist_page(row.id),
            music.MORE_SONGS: lambda: music.songs_page(row.id, row.params),
            music.MORE_ALBUMS: lambda: music.artist_albums(
                row.id, row.params, name, row.title.removeprefix("all ")
            ),
            music.MOODS: lambda: music.mood_playlists(row.id, row.title),
            music.MORE_RESULTS: lambda: music.search_more(row.id, row.params),
        }
        self.notify(f"opening {row.title}…", timeout=2)
        try:
            page = await asyncio.to_thread(loaders[row.kind])
        except Exception:
            self.notify(f"could not open {row.title}", severity="error")
            return
        if not any(section.results for section in page.sections):
            self.notify(f"nothing on {row.title}")
            return
        lst.push(page, *page_rows(page, back=True))

    def play_from(self, lst: TrackList, i: int, source: str) -> None:
        """A song on an album's page (or a list of songs): the rest of them from it, then a
        radio, as YouTube Music plays an album from a track."""
        tracks = music.tracks([row for row in lst.rows[i:] if row])
        self.launch(tracks[0].id, tracks[0].title, queue=tracks, then_radio=True, source=source)

    async def artist_radio(self, row: music.Result) -> None:
        self.notify(f"starting {row.title}…", timeout=3)
        try:
            tracks = await asyncio.to_thread(music.artist_radio, row.id)
        except Exception:
            tracks = []
        if not tracks:
            self.notify(f"could not start {row.title}", severity="error")
            return
        try:
            source = row.params or ARTIST_RADIO
            await control.start(tracks[0].id, queue=tracks, then_radio=True, source=source)
        except Failure as e:
            self.notify(str(e), severity="error")
            return
        self.action_tab("radio")


def run() -> None:
    MusicApp().run()
