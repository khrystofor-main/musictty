# musictty — context for Claude Code

Endless background music radio for the terminal. v0 is a Windows-only prototype;
a cross-platform Python rewrite with a Textual TUI is planned (see README roadmap).

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

## Gotchas

- A running mpv keeps the old Lua script: changes to `youtube-music.lua` take effect
  only after the radio is restarted.
- `radio.ps1` must stay UTF-8 **with BOM**, otherwise Windows PowerShell 5.1 breaks the
  Cyrillic comments. Check the BOM after every edit.
- Help text (`$HelpText`) is in English; code comments in v0 are in Russian.
- User-facing output: no dates or times in lists.

## Testing

Starting mpv plays music on the developer's machine, so test the PowerShell side
without it:
- syntax: `[Management.Automation.Language.Parser]::ParseFile(...)`;
- commands: run with redirected output (menus fall back to plain lists);
- if a data file is needed, create a temporary one only when the real one is missing,
  and delete it afterwards. Never touch the real `history.txt` / `liked.txt`.
