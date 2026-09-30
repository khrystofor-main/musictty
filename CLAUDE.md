# musictty — context for Claude Code

Endless background music radio for the terminal. Two versions live side by side:
- **v0** — the Windows-only PowerShell prototype in the repo root. Still in daily use
  (its folder is on the developer's PATH as `music`): don't move or break it.
- **Python version** — the cross-platform rewrite in `src/musictty/`, command `musictty`.
  Core radio, CLI and a first Textual UI are done (see README roadmap).

## Architecture (v0)

- `music.cmd` / `music.ps1` — thin wrappers that call `radio.ps1` (the folder is on PATH).
- `radio.ps1` — argument parsing, console menus, JSON IPC to mpv over the named pipe
  `youtube-music`. Each `music <command>` is a separate short-lived process.
- `youtube-music.lua` — runs inside a hidden `mpv.exe`: refills the playlist from the
  current track's mix (`RDAMVM<id>`) via yt-dlp, prefetches, publishes the list for
  `music list`, appends to `play-history.txt`.
- `youtube-music.conf` — mpv settings tuned for minimal memory.

Runtime data next to the scripts (git-ignored, personal):
`history.txt` (radio seeds, read by the bare `music` menu), `play-history.txt`
(every started track, `music history`), `liked.txt` (`music like/unlike/liked`),
`liked-queue.txt` (queue for the liked playlist, written by radio.ps1, read by the
Lua script at startup), `volume.txt`, `repeat-on`.

## Architecture (Python, `src/musictty/`)

mpv is the hub: it owns the IPC endpoint (`\\.\pipe\musictty` on Windows, a unix socket
in the user runtime dir elsewhere). Its clients:
- `cli.py` — the `musictty` command, a short-lived process per call. Strict parsing like
  v0; menus are replaced by numbers (`history 3`, `liked play 2`, `list back 1`).
  Playback commands go straight to mpv; `list back` sends `script-message musictty-jump`.
- `tui.py` — the Textual UI, opened by the bare `musictty` (without a terminal it prints
  the recent radios instead). It observes mpv's properties and the radio's list, keeps
  reconnecting as radios come and go, and quitting it leaves the music playing. Keys in
  the lists follow v0's menus (enter, ←/→, delete). The Search tab lists songs, albums and
  artists (`music.py`); an album plays as a queue that turns into a radio
  (`LaunchSpec.then_radio`: no mixes until its last track, then a normal radio). The
  Lyrics tab loads the playing track's lyrics (`music.lyrics`, cached) while it is open and
  highlights the sung line from the polled time-pos.
- `daemon.py` + `radio.py` — the background process (`python -m musictty.daemon`, a
  `LaunchSpec` as JSON on stdin). It starts mpv, and `Radio` is the port of
  `youtube-music.lua`: refills from the mix, prefetches the next track to a direct
  stream, trims played entries, publishes `user-data/musictty/list` and `.../source`.
  It exits when mpv quits.

Other modules: `control.py` (start/stop a radio, volume, repeat: shared by CLI and UI),
`ai.py` (AI radio: an OpenAI-compatible chat API turns a mood into "Artist - Title" songs;
presets DeepSeek and Nous Portal, chosen by which key env var is set; `control.ai_radio` finds
them with ytmusicapi and plays them as a then_radio queue), `music.py` (ytmusicapi: search, albums, artists' top songs; yt-dlp's search only gives bare
ids for albums and artists), `ipc.py` (async JSON IPC client), `player.py` (find/launch/stop mpv, spawn
the daemon), `youtube.py` (yt-dlp as a library; blocking, run in a thread),
`store.py` (data files, v0 import), `paths.py`, `mpv.conf`.

Data in the user data dir (platformdirs), same `date<TAB>id<TAB>title` lines as v0:
`seeds.tsv`, `plays.tsv`, `liked.tsv`, `settings.json` (volume, repeat), `radio.log`
(the daemon's log, rewritten on every radio start). `musictty import-v0` merges v0's files.

## Gotchas

- A running radio keeps the old code: changes to `youtube-music.lua` (v0) or to the
  daemon/radio modules take effect only after the radio is restarted.
- `radio.ps1` must stay UTF-8 **with BOM**, otherwise Windows PowerShell 5.1 breaks the
  Cyrillic comments. Check the BOM after every edit.
- Help text is in English in both versions; code comments are Russian in v0 and English
  in the Python version. User-facing messages are short and lowercase.
- User-facing output: no dates or times in lists.
- IPC events can be handled after the state has moved on: identify playlist entries by
  their mpv entry id (from `start-file`, `loadfile` replies), not by "current" or index.
- mpv leaves its unix socket file behind on exit; a refused connection means not running.
- mpv 0.37 doesn't know some options in `mpv.conf` (`load-select`, `media-controls`, ...):
  it logs and skips them. Keep them, they matter on current Windows builds.
- Windows-specific code (named pipes, process flags, stdin/log encodings) can't be run
  in a Linux container: keep it small, and check it in the Windows CI job.

## Testing

v0: starting mpv plays music on the developer's machine, so test the PowerShell side
without it:
- syntax: `[Management.Automation.Language.Parser]::ParseFile(...)`;
- commands: run with redirected output (menus fall back to plain lists);
- if a data file is needed, create a temporary one only when the real one is missing,
  and delete it afterwards. Never touch the real `history.txt` / `liked.txt`.

Python: `uv run pytest` and `uv run ruff check src tests && uv run ruff format --check src tests`.
- Tests isolate themselves via `MUSICTTY_HOME` (data dir) and `MUSICTTY_IPC` (IPC address)
  — see `tests/conftest.py`. Never run the real `musictty` against the real data dir.
- `tests/test_radio.py` drives the radio with an in-memory mpv (`tests/fakes.py`).
- `tests/test_tui.py` drives the UI headless with Textual's pilot, with the radio off and
  with a real mpv.
- `tests/test_player.py` uses a real silent mpv (`MUSICTTY_MPV_ARGS=--ao=null`) and local
  WAV files instead of YouTube; skipped when mpv is missing.
- YouTube and the AI APIs may be unreachable from the sandbox: yt-dlp and ytmusicapi calls
  are tested with fakes shaped like their documented results, the AI with a local fake
  OpenAI-compatible server (`tests/test_ai.py`).
- CI (`.github/workflows/tests.yml`) runs lint and the tests on Windows and Ubuntu, with a
  real mpv on both; `MUSICTTY_REQUIRE_MPV=1` turns a missing mpv into a failure there.
  This is where the Windows code actually gets exercised.
