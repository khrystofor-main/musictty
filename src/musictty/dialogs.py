"""Small modal dialogs of the terminal UI: a name, a playlist to save to, a yes/no question."""

from __future__ import annotations

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

NEW = "+ new playlist"

CSS = """
Ask, Pick, Choose, Confirm { align: center middle; }
.dialog {
    width: 60; max-width: 90%; height: auto; max-height: 80%;
    border: round $accent; background: $panel; padding: 0 1;
}
.dialog OptionList { height: auto; max-height: 16; border: none; background: $panel; }
.dialog .hint { color: $text-muted; }
"""


class Dialog(ModalScreen):
    DEFAULT_CSS = CSS
    # the player's keys don't reach through a dialog, except by typing in its input
    BINDINGS = [Binding("escape,q", "cancel", show=False)]

    def action_cancel(self) -> None:
        self.dismiss(None)


class Ask(Dialog):
    """A line of text, such as a playlist's name. None if cancelled or empty."""

    def __init__(self, prompt: str, value: str = "") -> None:
        super().__init__()
        self.prompt, self.value = prompt, value

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.prompt)
            yield Input(value=self.value)
            yield Static("enter ok · esc cancel", classes="hint")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)


class Pick(Dialog):
    """A playlist to save to: one of `names`, or a new one (its name is asked for).

    Returns (name, is_new), or None if cancelled.
    """

    def __init__(self, title: str, names: list[str]) -> None:
        super().__init__()
        self.title_text, self.names = title, names

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text, style="bold"))
            yield OptionList(*self.names, Option(Text(NEW, style="italic")))
            yield Static("enter pick · esc cancel", classes="hint")

    @on(OptionList.OptionSelected)
    def picked(self, event: OptionList.OptionSelected) -> None:
        if event.option_index < len(self.names):
            self.dismiss((self.names[event.option_index], False))
            return

        def named(name: str | None) -> None:
            if name:
                self.dismiss((name, True))

        self.app.push_screen(Ask("name of the new playlist"), named)


class Choose(Dialog):
    """One of a few options: returns its index, or None if cancelled."""

    def __init__(self, title: str, labels: list[str], current: int | None = None) -> None:
        super().__init__()
        self.title_text, self.labels, self.current = title, labels, current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(Text(self.title_text, style="bold"))
            marks = ["● " if i == self.current else "  " for i in range(len(self.labels))]
            yield OptionList(*(m + label for m, label in zip(marks, self.labels, strict=True)))
            yield Static("enter pick · esc cancel", classes="hint")

    def on_mount(self) -> None:
        if self.current is not None:
            self.query_one(OptionList).highlighted = self.current

    @on(OptionList.OptionSelected)
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option_index)


class Confirm(Dialog):
    """A yes/no question: True for y."""

    BINDINGS = [
        Binding("y", "answer(True)", show=False),
        Binding("n,escape,q", "answer(False)", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield Static("y yes · n no", classes="hint")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)
