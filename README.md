# musictty

Endless music radio in your terminal. Pick a track, and musictty keeps playing
similar music in the background: it follows the track's mix, prefetches the next
songs, and never stops until you tell it to.

> **Status: v0 prototype (Windows, PowerShell).** A cross-platform Python rewrite
> is in progress: the background radio and the command line already work, a full
> terminal UI is next, see [Python version](#python-version-preview) and [Roadmap](#roadmap).

## What it does

- **Radio from anything:** a search query, a track link, or a video ID
- **Runs in the background:** a hidden `mpv` process, controlled from any terminal
- **History:** every track you've played, across all radio sessions
- **Likes:** mark tracks with ♥ and play them as a looping playlist
- **Low footprint:** audio only, tiny buffers, no windows

```text
music search daft punk     # start a radio from the first matching song
music now                  # what's playing: Daft Punk — Digital Love · radio mix
music like                 # ♥ the current track
music liked                # browse liked tracks, play them as a playlist
music history              # recently played tracks across all radios
music next | prev | pause | play | stop
```

Run `music help` for the full list of commands.

## Install (Windows)

1. Install [mpv](https://mpv.io/installation/) and [yt-dlp](https://github.com/yt-dlp/yt-dlp):
   ```powershell
   winget install yt-dlp.yt-dlp
   ```
2. Clone this repository and add its folder to your `PATH`.
3. Run `music search <anything>`.

musictty looks for `mpv.exe` in `PATH`, `%ProgramFiles%\MPV Player` and
`%LOCALAPPDATA%\Programs\mpv`. A local yt-dlp build in a `yt-dlp/` folder next to
the scripts takes priority over the system one.

## Python version (preview)

The cross-platform rewrite (Windows, macOS, Linux) already plays radios from the
command line. Its command is `musictty`, so it doesn't clash with v0's `music`.

1. Install [mpv](https://mpv.io/installation/) 0.37 or newer.
2. Install musictty with [uv](https://docs.astral.sh/uv/):
   ```sh
   uv tool install git+https://github.com/khrystofor-main/musictty
   ```
   yt-dlp and the Deno runtime it needs for YouTube come along.
3. Optionally bring over v0's radios, history and likes: `musictty import-v0`
   (finds v0 on `PATH`, or pass its folder).

The commands are the same as in v0. Until the terminal UI arrives, the arrow-key menus
are replaced by numbers: `musictty history` prints a numbered list, `musictty history 3`
starts a radio from track 3. Run `musictty help` for the full list.

Listening data is kept in your user data folder (`%LOCALAPPDATA%\musictty`,
`~/.local/share/musictty` or `~/Library/Application Support/musictty`).

## How it works

```text
music.cmd / music.ps1
        │
        ▼
    radio.ps1  ── argument parsing, console menus, JSON IPC over a named pipe
        │
        ▼
  mpv (hidden) + youtube-music.lua
        └── refills the queue from the current track's mix via yt-dlp,
            prefetches ahead, writes play history
```

Listening data (`history.txt`, `play-history.txt`, `liked.txt`) is stored as plain
text next to the scripts and never leaves your machine.

The Python version keeps the same idea, with the radio logic moved from Lua to Python:

```text
musictty <command>  ──┐                    ┌── musictty.daemon (background)
  (short-lived CLI)   │   JSON IPC         │   refills the queue from the mix,
                      └──►  mpv (hidden) ◄─┘   prefetches ahead, writes history
```

mpv is the hub: the CLI sends playback commands straight to it and reads the track
list the background process publishes there.

## Roadmap

- [x] v0: background radio, history, likes (PowerShell + mpv + Lua)
- [x] Python rewrite, cross-platform: background radio, history, likes, command line
- [ ] Terminal UI ([Textual](https://textual.textualize.io/))
- [ ] Rich search: albums, artists, playlists; lyrics
- [ ] AI radio: describe a mood in plain words, and Claude builds the queue
- [ ] Album art in the terminal

## Built with Claude Code

This project is developed together with [Claude Code](https://claude.com/claude-code).
I make the product and architecture decisions and review every change; Claude
writes most of the code. [`CLAUDE.md`](CLAUDE.md) holds the project context the
agent works from, and commits co-authored by Claude are marked as such.

## Disclaimer

musictty is an independent project and is not affiliated with, endorsed by, or
sponsored by YouTube or Google. It is intended for personal use; please respect
the terms of service of the content you play.

## License

[MIT](LICENSE)
