import pytest
from fakes import tid

from musictty import cli, control, player
from musictty.cli import Call, Invalid, list_lines, parse
from musictty.models import Track
from musictty.store import Settings, Store

S, A, B = (tid(n) for n in range(3))


@pytest.mark.parametrize(
    ("argv", "call"),
    [
        ([], Call("ui")),
        (["recent"], Call("recent")),
        (["3"], Call("recent", number=3)),
        (["search", "daft", "punk"], Call("radio", text="daft punk", action="search")),
        (["search", "next"], Call("radio", text="next", action="search")),
        (["ai", "calm", "evening"], Call("ai", text="calm evening")),
        (["https://youtu.be/abcdefghijk"], Call("radio", text="https://youtu.be/abcdefghijk")),
        (
            ["https://music.youtube.com/watch?v=abc&x=1"],
            Call("radio", text="https://music.youtube.com/watch?v=abc&x=1"),
        ),
        ([S], Call("radio", text=S)),
        (["now"], Call("now")),
        (["vol+"], Call("vol+")),
        (["mem"], Call("mem")),
        (["repeat", "on"], Call("repeat", text="on")),
        (["list"], Call("list")),
        (["list", "2"], Call("list", number=2)),
        (["list", "back", "2"], Call("list", number=2, action="back")),
        (["history", "30"], Call("history", number=30)),
        (["liked", "play", "1"], Call("liked", number=1, action="play")),
        (["liked", "repeat", "1"], Call("liked", number=1, action="repeat")),
        (["liked", "remove", "4"], Call("liked", number=4, action="remove")),
        (["upnext"], Call("upnext")),
        (["upnext", "2"], Call("upnext", number=2)),
        (["upnext", "remove", "1"], Call("upnext", number=1, action="remove")),
        (["shuffle"], Call("shuffle")),
        (["repeat", "all", "on"], Call("repeat", text="on", action="all")),
        (["repeat", "all", "off"], Call("repeat", text="off", action="all")),
        (["seek", "+10"], Call("seek", number=10)),
        (["seek", "-10"], Call("seek", number=-10)),
        (["seek", "30"], Call("seek", number=30)),
        (["history", "queue", "2"], Call("history", number=2, action="queue")),
        (["history", "next", "2"], Call("history", number=2, action="next")),
        (["liked", "queue", "3"], Call("liked", number=3, action="queue")),
        (["liked", "next", "1"], Call("liked", number=1, action="next")),
        (["playlists"], Call("playlists")),
        (["playlists", "2"], Call("playlists", number=2)),
        (["playlists", "play", "1"], Call("playlists", number=1, action="play")),
        (["playlists", "add", "1"], Call("playlists", number=1, action="add")),
        (["playlists", "delete", "3"], Call("playlists", number=3, action="delete")),
        (["playlists", "new", "Road", "trip"], Call("playlists", text="Road trip", action="new")),
        (["import-v0"], Call("import-v0")),
        (["import-v0", "C:\\music"], Call("import-v0", text="C:\\music")),
    ],
)
def test_parse(argv, call):
    assert parse(argv) == call


@pytest.mark.parametrize(
    "argv",
    [
        ["search"],
        ["ai"],
        ["repeat"],
        ["repeat", "maybe"],
        ["now", "please"],
        ["daft", "punk"],
        ["Кириллица11"],  # 11 letters, but not an id
        ["https://example.com/watch?v=abcdefghijk"],
        ["0"],
        ["list", "0"],
        ["list", "back"],
        ["history", "back", "1"],
        ["history", "play", "1"],
        ["upnext", "back", "1"],
        ["shuffle", "now"],
        ["repeat", "all"],
        ["repeat", "all", "maybe"],
        ["seek"],
        ["seek", "ten"],
        ["seek", "1.5"],
        ["liked", "play"],
        ["playlists", "new"],
        ["playlists", "remove", "1"],
        ["playlists", "play"],
        ["liked", "shuffle", "1"],
    ],
)
def test_parse_rejects(argv):
    with pytest.raises(Invalid):
        parse(argv)


def test_invalid_input_touches_nothing(capsys, isolated):
    assert cli.main(["daft", "punk"]) == 1
    assert capsys.readouterr().out == "invalid input\n"
    assert not isolated.exists()


def test_commands_with_the_radio_off_are_quiet(capsys):
    for command in ("now", "next", "like", "list", "pause"):
        assert cli.main([command]) == 1
    assert cli.main(["stop"]) == 0
    assert capsys.readouterr().out == ""


def test_volume_and_repeat_are_remembered_with_the_radio_off():
    cli.main(["vol-"])
    cli.main(["vol-"])
    cli.main(["repeat", "on"])
    assert Store().settings() == Settings(volume=60, repeat=True)
    for _ in range(20):
        cli.main(["vol+"])
    assert Store().settings().volume == 130  # mpv's limit


def test_lists_are_numbered_newest_first(capsys):
    store = Store()
    for t in (Track(S, "one"), Track(A, "two")):
        store.add_seed(t)
        store.add_play(t)
        store.like(t)
    # the bare command with no terminal falls back to the list of recent radios, as in v0
    for command in ([], ["recent"], ["history"], ["liked"]):
        assert cli.main(command) == 0
        assert capsys.readouterr().out == " 1. two\n 2. one\n"


def test_picking_from_a_list(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "start_radio", lambda *a, **kw: started.append((a, kw)))
    store = Store()
    for t in (Track(S, "s"), Track(A, "a"), Track(B, "b")):
        store.add_seed(t)
        store.like(t)
    assert cli.main(["2"]) == 0
    assert cli.main(["liked", "1"]) == 0
    assert cli.main(["liked", "4"]) == 1  # out of range
    assert started == [((A,), {}), ((B,), {})]


def test_liked_playlist_starts_from_the_chosen_track_and_wraps(monkeypatch):
    started = []
    monkeypatch.setattr(cli, "start_radio", lambda *a, **kw: started.append((a, kw)))
    store = Store()
    tracks = [Track(S, "s"), Track(A, "a"), Track(B, "b")]
    for t in tracks:
        store.like(t)
    cli.main(["liked", "play", "2"])  # newest first: b, a, s
    cli.main(["liked", "repeat", "1"])
    assert started == [
        ((A,), {"queue": [tracks[1], tracks[0], tracks[2]], "repeat_one": False}),
        ((B,), {"queue": [tracks[2], tracks[1], tracks[0]], "repeat_one": True}),
    ]


def test_liked_remove(capsys):
    store = Store()
    store.like(Track(S, "s"))
    store.like(Track(A, "a"))
    assert cli.main(["liked", "remove", "1"]) == 0
    assert capsys.readouterr().out == "♡ a\n"
    assert store.liked() == [Track(S, "s")]


def test_list_marks():
    items = [
        {"entry": 3, "id": B, "title": "b", "current": False},
        {"entry": 2, "id": A, "title": "a", "current": True},
        {"entry": 1, "id": S, "title": "s", "current": False},
    ]
    assert list_lines(items, repeat=False, liked=set()) == [" 1.   b", " 2. ▶ a", " 3.   s"]
    assert list_lines(items, repeat=True, liked={S}) == [
        " 1.       b",
        " 2. ▶ ↻   a",
        " 3.     ♥ s",
    ]


def test_mpv_missing(monkeypatch, capsys):
    monkeypatch.setattr(player, "find_mpv", lambda: None)
    assert cli.main([S]) == 1
    assert capsys.readouterr().out == "mpv not found\n"


def test_link_without_a_track(capsys):
    assert cli.main(["https://www.youtube.com/playlist?list=PL123"]) == 1
    assert capsys.readouterr().out == "no track in this link\n"


def test_import_v0(tmp_path, capsys):
    v0 = tmp_path / "v0"
    v0.mkdir()
    (v0 / "liked.txt").write_text(f"2025-01-01 10:00\t{S}\ts\n", encoding="utf-8")
    assert cli.main(["import-v0", str(v0)]) == 0
    assert capsys.readouterr().out == "imported: 0 radios, 0 plays, 1 liked\n"
    assert cli.main(["import-v0", str(v0)]) == 1
    assert capsys.readouterr().out == "already imported\n"


def test_playlists(capsys, monkeypatch):
    started = []

    async def fake_start(seed=None, **kwargs):
        started.append((seed, kwargs))

    monkeypatch.setattr(control, "start", fake_start)
    assert cli.main(["playlists", "new", "Road", "trip"]) == 0
    assert cli.main(["playlists", "new", "road  trip"]) == 1  # taken
    assert cli.main(["playlists", "new", "Empty"]) == 0
    Store().add_to_playlist("Road trip", [Track(S, "s"), Track(A, "a")])
    assert capsys.readouterr().out == "made Road trip\n«road trip» already exists\nmade Empty\n"
    assert cli.main(["playlists"]) == 0
    assert capsys.readouterr().out == " 1. Road trip (2)\n 2. Empty (0)\n"
    assert cli.main(["playlists", "1"]) == 0
    assert capsys.readouterr().out == " 1. s\n 2. a\n"
    assert cli.main(["playlists", "play", "1"]) == 0
    assert cli.main(["playlists", "play", "2"]) == 1
    assert capsys.readouterr().out == "Empty is empty\n"
    assert cli.main(["playlists", "add", "1"]) == 1  # nothing is playing: quietly
    assert cli.main(["playlists", "delete", "2"]) == 0
    assert capsys.readouterr().out == "deleted Empty\n"
    assert [p.name for p in Store().playlists()] == ["Road trip"]
    queue = [Track(S, "s"), Track(A, "a")]
    assert started == [(S, {"queue": queue, "then_radio": True, "source": "playlist"})]
