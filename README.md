# musictty

Endless music radio in your terminal. Pick a track, and musictty keeps playing
similar music in the background: it follows the track's mix, prefetches the next
songs, and never stops until you tell it to. Windows, macOS and Linux.

## What it does

- **Radio from anything:** a search query, a track link, an album, or a mood for the AI
- **Runs in the background:** a hidden `mpv` process; the player UI and the commands
  are just remotes, and closing them leaves the music playing
- **History and likes:** every track you've played, and ♥ tracks as a looping playlist
- **Lyrics:** synced with the song when YouTube Music has them timed
- **Low footprint:** audio only, tiny buffers, no windows

## Install

1. Install [mpv](https://mpv.io/installation/) 0.37 or newer.
2. Install musictty with [uv](https://docs.astral.sh/uv/):
   ```sh
   uv tool install git+https://github.com/khrystofor-main/musictty
   ```
   yt-dlp and the Deno runtime it needs for YouTube come along. The command is
   `musictty`, and `music` as well.
3. Coming from the PowerShell prototype? `musictty import-v0` brings over its radios,
   history, likes, volume and repeat (it finds v0 on `PATH`, or pass its folder).

## The player

`musictty` on its own opens the player: what's playing, the current radio, recent radios,
history, liked tracks, search and lyrics.

Keys: `space` pause, `n`/`p` next and previous, `+`/`-` volume, `l` like, `r` repeat,
`s` stop, `/` search, `a` AI radio, `1`–`6` tabs, `q` quit (the music keeps playing).

In the lists, `enter` starts a radio from a track. `←` jumps back in the current radio; in
liked, `→` plays them in a loop from that track and `←` does the same with it on repeat,
`delete` removes it.

**Search** finds songs, albums and artists on YouTube Music: a song starts a radio, an album
plays in order and then turns into a radio from its last track, an artist shows their top
songs (`←` goes back to the results).

**Lyrics** (tab `6`) follow the song line by line when YouTube Music has them timed, and
show as plain text otherwise.

**AI radio** (`a`, or `musictty ai "calm electronic for an evening of work"`): a language
model picks a few songs for the mood, they play in order, and the radio goes on from the
last one. It works with any OpenAI-compatible API; set the key of one of the presets and
open a new terminal:

```powershell
setx DEEPSEEK_API_KEY "sk-..."     # DeepSeek (deepseek-chat)
setx NOUS_API_KEY "..."            # or Nous Portal (google/gemini-3.8-flash)
```

`MUSICTTY_AI_PROVIDER` picks one when both are set; `MUSICTTY_AI_URL`, `MUSICTTY_AI_MODEL`
and `MUSICTTY_AI_KEY` point it anywhere else. The key stays in your environment.

## Commands

Everything also works from the command line, with numbers where the player has lists:

```text
musictty search daft punk     # a radio from the first matching song
musictty now                  # Daft Punk — Digital Love · radio mix
musictty history              # recently played tracks; `musictty history 3` plays one
musictty liked play 2         # liked tracks in a loop, from the second one down
musictty next | prev | pause | play | stop | like | mem
```

Run `musictty help` for the full list.

Listening data is kept in your user data folder (`%LOCALAPPDATA%\musictty`,
`~/.local/share/musictty` or `~/Library/Application Support/musictty`) and never leaves
your machine.

## How it works

```text
musictty (player UI) ──┐                    ┌── musictty.daemon (background)
musictty <command>  ───┤   JSON IPC         │   refills the queue from the mix,
                       └──►  mpv (hidden) ◄─┘   prefetches ahead, writes history
```

mpv is the hub: the player and the commands send playback commands straight to it and
read the track list the background process publishes there.

The first version was a Windows-only PowerShell prototype; it lives on in
[`legacy/`](legacy/) (put that folder on `PATH` instead of the repository root if you
still use it).

## Roadmap

- [x] v0: background radio, history, likes (PowerShell + mpv + Lua, now in `legacy/`)
- [x] Python rewrite, cross-platform: background radio, history, likes, command line
- [x] Terminal UI ([Textual](https://textual.textualize.io/)): player, radios, history, likes, search
- [x] Rich search: songs, albums, artists
- [x] Lyrics, synced with the song
- [ ] Playlists in search
- [x] AI radio: describe a mood in plain words, and a language model picks the songs
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
