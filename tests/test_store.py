import pytest

from musictty.models import Track
from musictty.store import (
    LIKED,
    PLAYLISTS,
    PLAYS,
    SEEDS,
    SETTINGS,
    AlreadyImported,
    Playlist,
    PlaylistError,
    Settings,
    Store,
    import_v0,
)

A, B, C = (Track(f"track{n:06d}", f"Artist — Song {n}") for n in range(3))


@pytest.fixture
def store(isolated):
    return Store()


def test_recent_seeds_are_distinct_newest_first(store):
    for t in (A, B, A, C):
        store.add_seed(t)
    assert store.recent_seeds() == [C, A, B]
    assert store.recent_seeds(limit=2) == [C, A]


def test_recent_plays_keep_repeats(store):
    for t in (A, B, A):
        store.add_play(t)
    assert store.recent_plays() == [A, B, A]
    assert store.recent_plays(limit=2) == [A, B]


def test_trim_plays(store):
    for n in range(50):
        store.add_play(Track(f"track{n:06d}", "x" * 50))
    store.trim_plays(max_bytes=10_000)  # still small: untouched
    assert len(store.recent_plays(100)) == 50
    store.trim_plays(max_bytes=100, keep=10)
    plays = store.recent_plays(100)
    assert [t.id for t in plays] == [f"track{n:06d}" for n in range(49, 39, -1)]


def test_likes(store):
    assert store.like(A)
    assert store.like(B)
    assert not store.like(A)
    assert store.liked() == [A, B]
    assert store.unlike(A.id)
    assert not store.unlike(A.id)
    assert store.liked() == [B]
    assert store.unlike(B.id)
    assert store.liked() == []


def test_ids_are_case_sensitive(store):
    lower, upper = Track("abcdefghijk", "lower"), Track("ABCDEFGHIJK", "upper")
    store.like(lower)
    store.like(upper)
    store.unlike("abcdefghijk")
    assert store.liked() == [upper]


def test_titles_cannot_break_lines(store):
    store.like(Track(A.id, "one\ttwo\nthree"))
    assert store.liked() == [Track(A.id, "one two three")]


def test_settings(store):
    assert store.settings() == Settings(volume=70, repeat=False)
    store.save_settings(Settings(volume=85, repeat=True))
    assert store.settings() == Settings(volume=85, repeat=True)
    store.path(SETTINGS).write_text("{broken", encoding="utf-8")
    assert store.settings() == Settings()


def write_v0(folder, name, lines, bom=False):
    text = "".join(line + "\r\n" for line in lines)
    (folder / name).write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode())


def test_import_v0(store, tmp_path):
    v0 = tmp_path / "v0"
    v0.mkdir()
    write_v0(v0, "history.txt", [f"2025-01-01 10:00\t{A.id}\t{A.title}"], bom=True)
    write_v0(v0, "play-history.txt", [f"2025-01-01 10:0{n}\t{A.id}\t{A.title}" for n in range(3)])
    write_v0(v0, "liked.txt", [f"2025-01-01 10:00\t{A.id}\t{A.title}", "garbage", ""])
    (v0 / "volume.txt").write_text("55\r\n")
    (v0 / "repeat-on").write_text("")
    # something already recorded by the new version stays, after the older v0 records
    store.add_seed(B)
    store.like(B)
    store.like(A)

    counts = import_v0(store, v0)

    assert counts == {SEEDS: 1, PLAYS: 3, LIKED: 1, SETTINGS: 1}
    assert store.recent_seeds() == [B, A]
    assert store.recent_plays() == [A, A, A]
    assert store.liked() == [A, B]
    assert store.settings() == Settings(volume=55, repeat=True)
    with pytest.raises(AlreadyImported):
        import_v0(store, v0)


def test_import_v0_keeps_own_settings(store, tmp_path):
    v0 = tmp_path / "v0"
    v0.mkdir()
    (v0 / "volume.txt").write_text("55")
    store.save_settings(Settings(volume=90))
    assert import_v0(store, v0) == {}
    assert store.settings().volume == 90


def test_import_v0_empty_folder_is_not_marked(store, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert import_v0(store, empty) == {}
    import_v0(store, empty)  # a wrong folder can be corrected: no AlreadyImported


def test_playlists(isolated):
    store = Store()
    assert store.playlists() == []
    store.create_playlist("  Evening   jazz ")  # spaces are tidied
    store.create_playlist("Работа")
    with pytest.raises(PlaylistError):
        store.create_playlist("evening jazz")  # names are unique, whatever the case
    with pytest.raises(PlaylistError):
        store.create_playlist("   ")
    a, b, c = A, B, C
    assert store.add_to_playlist("Evening jazz", [a, b, a]) == 2
    assert store.add_to_playlist("Evening jazz", [b, c]) == 1  # b is there already
    store.move_in_playlist("Evening jazz", c.id, -1)
    store.move_in_playlist("Evening jazz", a.id, -1)  # at the top already
    store.remove_from_playlist("Evening jazz", b.id)
    assert store.playlists() == [Playlist("Evening jazz", [a, c]), Playlist("Работа", [])]
    store.delete_playlist("Evening jazz")
    assert [p.name for p in store.playlists()] == ["Работа"]
    with pytest.raises(PlaylistError):
        store.add_to_playlist("Evening jazz", [a])


def test_broken_playlists_file_is_empty_not_fatal(isolated):
    store = Store()
    store.path(PLAYLISTS).parent.mkdir(parents=True, exist_ok=True)
    store.path(PLAYLISTS).write_text("{not json", encoding="utf-8")
    assert store.playlists() == []
    store.path(PLAYLISTS).write_text('[{"name": "ok", "tracks": [["x", "y"]]}, {"nope": 1}, 5]')
    assert store.playlists() == [Playlist("ok", [Track("x", "y")])]
