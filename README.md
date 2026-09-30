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
history, liked tracks, search, lyrics, what's up next, your playlists and YouTube Music's
Home and Explore. The bar at the bottom shows the keys: the player's, then those of the list
you're in (a narrow terminal gets another line or two).

Keys: `space` pause, `n`/`p` next and previous, `+`/`-` volume, `,`/`.` seek 10 seconds
back and forward, `l` like, `r` repeat the track, `R` repeat the whole queue, `x` shuffle
what's up next, `s` stop, `z` sleep timer, `Q` audio quality, `/` search, `a` AI radio,
`1`–`9` tabs, `q` quit (the music keeps playing).

In the lists, `enter` starts a radio from a track, `e` adds it to the queue, `E` plays it
next, `S` saves it to one of your playlists (an album or a playlist from the search goes in
whole) and `g` goes to its artist or album (outside the lists: the track that's playing).
`←` jumps back in the current radio; in liked, `→` plays them in a loop from that track and
`←` does the same with it on repeat, `delete` removes it.

**Up next** (tab `7`) is what plays after the current track: the queue first (what you added,
the rest of an album or a playlist), then the radio's own picks. `enter` plays a track now
(the ones before it still come next), `E` moves it to the top, `shift+↑`/`shift+↓` move it,
`delete` removes it. The radio keeps adding picks once the queue has played out; with repeat
all it adds nothing and the queue starts over.

**Search** (`/`) suggests as you type, like YouTube Music: `↓` goes into the suggestions and
`enter` searches the one under the cursor. It finds songs, albums, artists and playlists,
eight of each ("all songs →" and the like show more): a song starts a radio, an album or a
playlist plays in order and then turns into a radio from its last track. `→` opens a
playlist or an album (`enter` on a track plays from there) and an artist's page: their
radio, top songs, albums, singles and similar artists, with the full lists one `→` further.
`←` goes back.

**Playlists** (tab `8`) are your own, kept on your machine: `+ new playlist` makes one, `S`
on any track adds it, `enter` plays one (then a radio goes on), `→` opens it to reorder
(`shift+↑`/`shift+↓`) or remove tracks, `delete` on a playlist deletes it (after asking).

**Home** (tab `9`) is YouTube Music's Home and Explore, as it shows them without an account:
its rows (quick picks, mixes and the like), then new releases, trending songs, the charts and
their top artists, moods and genres (`→` on one lists its playlists). It opens like the
search: `enter` plays, `→` opens, `←` goes back. The charts are global;
`MUSICTTY_CHARTS=DE` (any country code) picks a country's.

**Sleep timer** (`z`): the radio stops in 15 minutes to an hour and a half, fading out over
the last half minute, or after the track that's playing. It runs in the background process,
so it works with the player closed. **Audio quality** (`Q`): low (about 50–70 kbit/s), normal
(about 130–160 kbit/s, the default) or high (the best stream YouTube has); it applies to the
tracks to come and to every radio after.

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
musictty history queue 3      # add it to the queue instead (`history next 3`: play it next)
musictty upnext               # what's coming up; `musictty upnext remove 2` takes one out
musictty liked play 2         # liked tracks in a loop, from the second one down
musictty playlists            # your playlists; `playlists play 1`, `playlists add 1`
musictty seek +30 | shuffle | repeat all on
musictty sleep 30 | sleep end | sleep off | quality low
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
- [x] Artist and album pages
- [x] Lyrics, synced with the song
- [x] Queue: up next, play next, add to queue, shuffle, repeat all, seek
- [x] Playlists: YouTube Music's in search, your own in the player
- [x] Home and Explore: new releases, charts, moods and genres
- [x] Search suggestions
- [x] Sleep timer, audio quality
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
