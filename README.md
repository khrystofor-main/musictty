# musictty

Endless music radio in your terminal. Pick a track, and musictty keeps playing
similar music in the background: it follows the track's mix, prefetches the next
songs, and never stops until you tell it to.

> **Status: v0 prototype (Windows, PowerShell).** A cross-platform Python rewrite
> with a full terminal UI is in progress, see [Roadmap](#roadmap).

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

## Roadmap

- [x] v0: background radio, history, likes (PowerShell + mpv + Lua)
- [ ] Python rewrite with a terminal UI ([Textual](https://textual.textualize.io/)), cross-platform
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
