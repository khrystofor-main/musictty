<#
  Бесконечное радио YouTube Music через mpv в фоне.

  Все команды с описанием: music help (текст — в $HelpText ниже).
#>
param(
    [Parameter(Position = 0)][string]$Seed,
    # слова после первого — запрос для music search Daft Punk (можно без кавычек)
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = 'Stop'
# работаем молча: при непредвиденной ошибке (например, радио не запущено) просто выходим с кодом 1
trap { exit 1 }
$Pipe = 'youtube-music'
$Dir  = $PSScriptRoot
$History = Join-Path $Dir 'history.txt'
# все проигранные треки всех запусков (дата <TAB> id <TAB> название) — пишет youtube-music.lua, читает music history
$Plays = Join-Path $Dir 'play-history.txt'
# понравившиеся треки (дата <TAB> id <TAB> название) — music like / unlike / liked
$Liked = Join-Path $Dir 'liked.txt'
# очередь для плейлиста понравившихся (→ / ← в music liked) — radio.ps1 пишет, youtube-music.lua читает при старте
$QueueFile = Join-Path $Dir 'liked-queue.txt'
# повтор — глобальная настройка: файл есть — повтор включён, в том числе для каждого нового радио
$RepeatFile = Join-Path $Dir 'repeat-on'
# громкость — тоже глобальная: последнее значение из vol+/vol- хранится здесь и применяется к каждому новому радио
$VolumeFile = Join-Path $Dir 'volume.txt'

function Get-SavedVolume {
    try { [int](Get-Content $VolumeFile -TotalCount 1 -ErrorAction Stop) } catch { 70 }   # 70 — как в youtube-music.conf
}

# vol+ / vol-: меняем играющее радио и запоминаем; если радио выключено — меняем только запомненное
function Step-Volume([int]$Delta) {
    if (Test-Path "\\.\pipe\$Pipe") {
        Send-Mpv @('add', 'volume', $Delta) | Out-Null
        $vol = [int][Math]::Round((Send-Mpv @('get_property', 'volume')))
    } else {
        $vol = [Math]::Max(0, [Math]::Min(130, (Get-SavedVolume) + $Delta))   # 130 — предел громкости mpv
    }
    Set-Content $VolumeFile $vol
}

$HelpText = @"
youtube music radio — commands:

  music search query     new radio from a track found on youtube music
  music link             new radio from a track link (music.youtube.com, youtube.com, youtu.be)

  music                  start history: ↑/↓ select, enter or → start, esc exit

  music list             current and played tracks: ← jump back here (queue is kept),
                         → or enter new radio from here, esc exit
  music history          last 30 played tracks of all radios: ↑/↓ select,
                         enter or → new radio from here, esc exit
  music liked            liked tracks as a playlist: ↑/↓ select, → play liked from here (in a loop),
                         ← same with this track on repeat, enter new radio from here,
                         delete remove from liked, esc exit
  music like             like current track (shown as ♥ in now and list)
  music unlike           remove current track from liked
  music now              what's playing and from where (radio mix or playlist)
  music next             next track
  music prev             previous track
  music pause            pause
  music play             resume
  music repeat on        repeat current track (shown as ↻ in now and list)
  music repeat off       stop repeating
  music vol+ / vol-      volume ±5
  music stop             stop the radio

  music mem              player memory usage
  music update           update yt-dlp if the radio stops finding tracks
  music help             this list
"@

$Commands = 'now', 'next', 'prev', 'pause', 'play', 'repeat', 'vol+', 'vol-', 'stop', 'list', 'history', 'liked', 'like', 'unlike', 'mem', 'update', 'help'

function Stop-Error([string]$message) { Write-Host $message; exit 1 }
function Stop-Invalid { Write-Host 'invalid input'; exit 1 }

# Строгий разбор: проходит только то, что есть в списке, — команда, search <запрос>,
# ссылка или ID трека. Всё остальное — invalid input, радио не трогаем.
$ForceSearch = $false
if ($Seed -eq 'search') {
    if (-not $Rest) { Stop-Invalid }
    $Seed = $Rest -join ' '; $ForceSearch = $true
} elseif ($Seed) {
    $known = ($Commands -contains $Seed) -or $Seed -match '^[\w-]{11}$' -or
             $Seed -match '^https?://([\w-]+\.)*(youtube\.com|youtu\.be)/'
    if (-not $known) { Stop-Invalid }
    # единственная команда с параметром: music repeat on | off (голое music repeat — ошибка)
    if ($Seed -eq 'repeat') {
        if (@($Rest).Count -ne 1 -or $Rest[0] -notin 'on', 'off') { Stop-Invalid }
        $RepeatOn = $Rest[0] -eq 'on'
    } elseif ($Rest) { Stop-Invalid }
}
if ($Seed -eq 'help' -and -not $ForceSearch) { $HelpText; return }
# последние 10 разных начальных треков, новые сверху (строки: дата <TAB> id <TAB> название)
function Get-History {
    if (-not (Test-Path $History)) { return @() }
    $seen = @{}
    $lines = [IO.File]::ReadAllLines($History, [Text.Encoding]::UTF8)
    [array]::Reverse($lines)
    $lines | ForEach-Object {
        $date, $id, $title = $_ -split "`t", 3
        if ($id -and -not $seen[$id]) { $seen[$id] = $true; [pscustomobject]@{ Date = $date; Id = $id; Title = $title } }
    } | Select-Object -First 10
}

# понравившиеся треки в порядке добавления, без повторов. id YouTube различают регистр — сравниваем точно
function Get-Liked {
    if (-not (Test-Path $Liked)) { return }
    $seen = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    [IO.File]::ReadAllLines($Liked, [Text.Encoding]::UTF8) | ForEach-Object {
        $null, $id, $title = $_ -split "`t", 3
        if ($id -and $seen.Add($id)) { [pscustomobject]@{ Id = $id; Title = $title } }
    }
}

function Get-LikedIds {
    $set = New-Object 'Collections.Generic.HashSet[string]' ([StringComparer]::Ordinal)
    Get-Liked | ForEach-Object { [void]$set.Add($_.Id) }
    , $set
}

function Add-Liked([string]$Id, [string]$Title) {
    if ((Get-LikedIds).Contains($Id)) { return }
    $line = (Get-Date -Format 'yyyy-MM-dd HH:mm') + "`t$Id`t$Title`n"
    [IO.File]::AppendAllText($Liked, $line, (New-Object Text.UTF8Encoding $false))
}

function Remove-Liked([string]$Id) {
    if (-not (Test-Path $Liked)) { return }
    # @() — чтобы после удаления последнего трека получился пустой массив, а не $null
    $keep = [string[]]@([IO.File]::ReadAllLines($Liked, [Text.Encoding]::UTF8) | Where-Object { ($_ -split "`t", 3)[1] -cne $Id })
    [IO.File]::WriteAllLines($Liked, $keep, (New-Object Text.UTF8Encoding $false))
}

# меню в консоли: ↑/↓ — выбор, Enter — 'new', → — 'right' (где не различают — тоже новое радио), ← — 'jump' (если -AllowLeft),
# Delete — 'delete' (если -AllowDelete), Esc — выход. Возвращает @{ Index; Action } или $null
function Select-Menu([string[]]$Lines, [switch]$AllowLeft, [switch]$AllowDelete, [int]$Start = 0) {
    $n = $Lines.Count
    $w = [Console]::WindowWidth - 1
    # сначала резервируем строки, чтобы прокрутка консоли не сдвинула меню
    1..$n | ForEach-Object { Write-Host '' }
    $top = [Console]::CursorTop - $n
    $sel = [Math]::Max(0, [Math]::Min($Start, $n - 1))
    $cursor = [Console]::CursorVisible
    [Console]::CursorVisible = $false
    try {
        while ($true) {
            [Console]::SetCursorPosition(0, $top)
            for ($i = 0; $i -lt $n; $i++) {
                $text = $Lines[$i]
                if ($text.Length -gt $w) { $text = $text.Substring(0, $w) }
                if ($i -eq $sel) { Write-Host $text.PadRight($w) -ForegroundColor Black -BackgroundColor Gray }
                else             { Write-Host $text.PadRight($w) }
            }
            switch ([Console]::ReadKey($true).Key) {
                'UpArrow'    { $sel = ($sel - 1 + $n) % $n }
                'DownArrow'  { $sel = ($sel + 1) % $n }
                'Home'       { $sel = 0 }
                'End'        { $sel = $n - 1 }
                'Enter'      { return @{ Index = $sel; Action = 'new' } }
                'RightArrow' { return @{ Index = $sel; Action = 'right' } }
                'LeftArrow'  { if ($AllowLeft) { return @{ Index = $sel; Action = 'jump' } } }
                'Delete'     { if ($AllowDelete) { return @{ Index = $sel; Action = 'delete' } } }
                'Escape'     { return $null }
            }
        }
    } finally {
        # стираем меню и возвращаем курсор
        [Console]::SetCursorPosition(0, $top)
        1..$n | ForEach-Object { Write-Host (' ' * $w) }
        [Console]::SetCursorPosition(0, $top)
        [Console]::CursorVisible = $cursor
    }
}

if (-not $Seed) {
    $list = @(Get-History)
    if ($list.Count -eq 0) { return }
    # вывод перенаправлен (в файл, в другую команду) — просто печатаем список
    if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) {
        for ($i = 0; $i -lt $list.Count; $i++) { '{0,2}. {1}' -f ($i + 1), $list[$i].Title }
        return
    }
    $lines = for ($i = 0; $i -lt $list.Count; $i++) { '{0,2}. {1}' -f ($i + 1), $list[$i].Title }
    $pick = Select-Menu $lines
    if (-not $pick) { return }
    $Seed = $list[$pick.Index].Id
}

# свежий PATH из реестра (winget мог дописать его уже после открытия консоли) + типичные папки mpv.
# Первой идёт своя сборка yt-dlp из папки радио (не один exe, а папка — стартует на ~0,8 с быстрее);
# mpv и youtube-music.lua наследуют этот PATH и тоже берут её.
$env:Path = @(
    "$Dir\yt-dlp"
    [Environment]::GetEnvironmentVariable('Path', 'Machine')
    [Environment]::GetEnvironmentVariable('Path', 'User')
    "$env:ProgramFiles\MPV Player"
    "$env:LOCALAPPDATA\Programs\mpv"
) -join ';'

function Send-Mpv([object[]]$Command) {
    $client = New-Object System.IO.Pipes.NamedPipeClientStream('.', $Pipe, 'InOut')
    try { $client.Connect(1000) } catch { throw 'Радио не запущено.' }
    $w = New-Object System.IO.StreamWriter($client); $w.AutoFlush = $true
    $r = New-Object System.IO.StreamReader($client)
    $w.WriteLine((@{ command = $Command; request_id = 1 } | ConvertTo-Json -Compress))
    # пропускаем события, ждём ответ на наш запрос
    while ($line = $r.ReadLine()) {
        $msg = $line | ConvertFrom-Json
        if ($msg.request_id -eq 1) { $client.Dispose(); return $msg.data }
    }
    $client.Dispose()
}

# выключить радио наверняка: вежливо через канал, а если плеер не отвечает или завис — принудительно
function Stop-Radio {
    if (Test-Path "\\.\pipe\$Pipe") { try { Send-Mpv @('quit') | Out-Null } catch { } }
    # быстрая проверка: если mpv вообще не запущен, медленный запрос к списку процессов не нужен
    if (-not (Get-Process mpv, mpv.com -ErrorAction SilentlyContinue)) { return }
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='mpv.exe' or Name='mpv.com'" |
               Where-Object { $_.CommandLine -match "pipe\\$Pipe(`"|\s|$)" } |
               ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    foreach ($p in $procs) {
        if (-not $p.WaitForExit(1500)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; $p.WaitForExit(2000) | Out-Null }
    }
}

# music find next — это поиск слова "next", а не команда
# включён ли повтор текущего трека (loop-file = inf)
function Test-Repeat {
    $loop = Send-Mpv @('get_property', 'loop-file')
    [bool]($loop -and $loop -ne 'no')
}

# текущий трек из списка youtube-music.lua (id и "исполнитель — название", как в music list)
function Get-CurrentTrack {
    @(Send-Mpv @('get_property', 'user-data/youtube-music/list')) | Where-Object { $_.current } | Select-Object -First 1
}

switch ($(if ($ForceSearch) { '' } else { $Seed })) {
    'now'   {
        # название из списка youtube-music.lua; если его ещё нет — из плеера
        $cur = Get-CurrentTrack
        $title = if ($cur -and $cur.title) { $cur.title } else { Send-Mpv @('get_property', 'media-title') }
        $mark = ''
        if (Test-Repeat) { $mark += '↻ ' }
        if ($cur -and $cur.id -and (Get-LikedIds).Contains($cur.id)) { $mark += '♥ ' }
        # что играет — радио или плейлист; старый youtube-music.lua (запущен до обновления) не сообщает — это всегда радио
        $source = Send-Mpv @('get_property', 'user-data/youtube-music/source')
        if (-not $source) { $source = 'radio mix' }
        "$mark$title · $source"
        return
    }
    # music like / unlike — добавить текущий трек в понравившиеся или убрать; печатаем, какой именно
    'like'   {
        $cur = Get-CurrentTrack
        if (-not $cur -or -not $cur.id) { exit 1 }
        Add-Liked $cur.id $cur.title
        "♥ $($cur.title)"
        return
    }
    'unlike' {
        $cur = Get-CurrentTrack
        if (-not $cur -or -not $cur.id) { exit 1 }
        Remove-Liked $cur.id
        "♡ $($cur.title)"
        return
    }
    # понравившиеся как плейлист, новые сверху: → — играть их по кругу с выбранного вниз,
    # ← — то же, но выбранный трек на повторе (только в этом запуске), Enter — радио от трека,
    # Delete — убрать из понравившихся
    'liked' {
        $items = @(Get-Liked); [array]::Reverse($items)
        if ($items.Count -eq 0) { return }
        $number = { for ($i = 0; $i -lt $items.Count; $i++) { '{0,2}. {1}' -f ($i + 1), $items[$i].Title } }
        if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) { & $number; return }
        $start = 0
        while ($true) {
            $pick = Select-Menu @(& $number) -AllowLeft -AllowDelete -Start $start
            if (-not $pick) { return }
            if ($pick.Action -ne 'delete') { break }
            Remove-Liked $items[$pick.Index].Id
            $items = @(Get-Liked); [array]::Reverse($items)
            if ($items.Count -eq 0) { return }
            $start = $pick.Index
        }
        $sel = $pick.Index
        $Seed = $items[$sel].Id
        if ($pick.Action -ne 'new') {
            # очередь: с выбранного до конца, потом начало списка — по кругу это и есть "с выбранного вниз"
            $Queue = @($items[$sel..($items.Count - 1)])
            if ($sel -gt 0) { $Queue += $items[0..($sel - 1)] }
            $RepeatOne = $pick.Action -eq 'jump'
            $QueueName = 'liked playlist'   # так его покажет music now
        }
        break
    }
    # music repeat on | off — повтор текущего трека. Режим держится и после next — как "повтор одного" в плеерах
    'repeat' {
        # сначала запоминаем (сработает и при выключенном радио), потом применяем к играющему
        if ($RepeatOn) { New-Item -ItemType File $RepeatFile -Force | Out-Null }
        else { Remove-Item $RepeatFile -ErrorAction SilentlyContinue }
        if (Test-Path "\\.\pipe\$Pipe") {
            Send-Mpv @('set_property', 'loop-file', $(if ($RepeatOn) { 'inf' } else { 'no' })) | Out-Null
        }
        return
    }
    'next'  { Send-Mpv @('playlist-next', 'force') | Out-Null; return }
    'prev'  { Send-Mpv @('playlist-prev') | Out-Null; return }
    'pause' { Send-Mpv @('set_property', 'pause', $true) | Out-Null; return }
    'play'  { Send-Mpv @('set_property', 'pause', $false) | Out-Null; return }
    'vol+'  { Step-Volume 5; return }
    'vol-'  { Step-Volume -5; return }
    'stop'  { Stop-Radio; return }
    # текущий и проигранные треки: ← — вернуться сюда (очередь сохраняется), → / Enter — новое радио отсюда
    'list'  {
        $items = @(Send-Mpv @('get_property', 'user-data/youtube-music/list'))
        if ($items.Count -eq 0) { return }
        # новые сверху, старые ниже; после отката над текущим видны треки, прослушанные до отката
        [array]::Reverse($items)
        # ▶ — текущий, ↻ у текущего — включён повтор, ♥ — в понравившихся.
        # Колонку ↻ / ♥ добавляем, только если она кому-то нужна; пустые места забиваем пробелами, чтобы названия шли ровно
        $repeat = Test-Repeat
        $likedIds = Get-LikedIds
        $anyLiked = @($items | Where-Object { $_.id -and $likedIds.Contains($_.id) }).Count -gt 0
        $lines = foreach ($it in $items) {
            $mark = if ($it.current) { '▶ ' } else { '  ' }
            if ($repeat)   { $mark += if ($it.current) { '↻ ' } else { '  ' } }
            if ($anyLiked) { $mark += if ($it.id -and $likedIds.Contains($it.id)) { '♥ ' } else { '  ' } }
            $mark + $it.title
        }
        if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) { $lines; return }
        $cur = 0; for ($i = 0; $i -lt $items.Count; $i++) { if ($items[$i].current) { $cur = $i } }
        $pick = Select-Menu $lines -AllowLeft -Start $cur
        if (-not $pick) { return }
        $it = $items[$pick.Index]
        if ($pick.Action -eq 'jump') {
            if (-not $it.current) { Send-Mpv @('script-message', 'youtube-music-jump', "$($it.entry)") | Out-Null }
            return
        }
        if (-not $it.id) { return }
        $Seed = $it.id   # дальше — обычный запуск нового радио от этого трека
        break
    }
    # последние 30 проигранных треков всех запусков, новые сверху; → / Enter — новое радио от трека
    'history' {
        if (-not (Test-Path $Plays)) { return }
        $items = @([IO.File]::ReadAllLines($Plays, [Text.Encoding]::UTF8) | ForEach-Object {
            $null, $id, $title = $_ -split "`t", 3
            if ($id) { [pscustomobject]@{ Id = $id; Title = $title } }
        } | Select-Object -Last 30)
        if ($items.Count -eq 0) { return }
        [array]::Reverse($items)
        $lines = for ($i = 0; $i -lt $items.Count; $i++) { '{0,2}. {1}' -f ($i + 1), $items[$i].Title }
        if ([Console]::IsInputRedirected -or [Console]::IsOutputRedirected) { $lines; return }
        $pick = Select-Menu $lines
        if (-not $pick) { return }
        $Seed = $items[$pick.Index].Id
        break
    }
    # YouTube периодически ломает старые версии — обновляем свою сборку yt-dlp
    'update' { & yt-dlp -U 2>&1 | ForEach-Object { "$_".ToLower() }; return }
    # готовыми строками, а не таблицей: таблица PowerShell выводится с задержкой и теряется при выходе
    'mem'   {
        Get-Process mpv -ErrorAction SilentlyContinue | ForEach-Object {
            'private {0:N1} mb, working set {1:N1} mb' -f ($_.PrivateMemorySize64 / 1MB), ($_.WorkingSet64 / 1MB)
        }
        return
    }
}

# --- запуск нового радио ---
# yt-dlp пишет в UTF-8 — читаем так же, иначе названия на кириллице превратятся в кракозябры
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
$Format = (Get-Content "$Dir\youtube-music.conf" | Where-Object { $_ -match '^ytdl-format=(.+)$' } | Select-Object -First 1) -replace '^ytdl-format=', ''
if (-not $Format) { $Format = 'bestaudio' }

# один вызов yt-dlp: id, название и сразу прямая ссылка на аудиопоток — mpv не ждёт ещё один вызов
function Resolve-Track([string]$Url, [switch]$Search) {
    $a = @('--quiet', '--no-warnings', '--encoding', 'utf-8', '-f', $Format,
           '--print', "%(id)s`t%(uploader,channel|)s`t%(title)s`t%(http_headers.User-Agent|)s", '--print', 'urls')
    $a += if ($Search) { '--playlist-items', '1' } else { '--no-playlist' }
    $out = @(yt-dlp @a $Url 2>$null)
    if ($out.Count -ne 2 -or $out[1] -notmatch '^https?://') { return $null }
    $id, $artist, $title, $ua = $out[0] -split "`t", 4
    if ($id -notmatch '^[\w-]{11}$') { return $null }
    [pscustomobject]@{ Id = $id; Title = $(if ($artist) { "$artist — $title" } else { $title }); Ua = $ua; Stream = $out[1] }
}

if ($Seed -match '(?:[?&]v=|youtu\.be/)([\w-]{11})' -or (-not $ForceSearch -and $Seed -match '^([\w-]{11})$')) {
    $id = $Matches[1]
    $track = Resolve-Track "https://music.youtube.com/watch?v=$id"
} else {
    $q = [uri]::EscapeDataString($Seed)
    $track = Resolve-Track "https://music.youtube.com/search?q=$q#songs" -Search
    if ($track) { $id = $track.Id }
    else {
        # первым в выдаче оказался микс или плейлист — ищем первый ID именно трека
        $id = yt-dlp --flat-playlist --quiet --no-warnings --playlist-end 5 --print id `
                     "https://music.youtube.com/search?q=$q#songs" 2>$null |
              Where-Object { $_ -match '^[\w-]{11}$' } | Select-Object -First 1
        if (-not $id) {
            $id = yt-dlp --flat-playlist --quiet --no-warnings --print id "ytsearch1:$Seed" 2>$null | Select-Object -First 1
        }
        if (-not $id) { Stop-Error 'nothing found' }
    }
}

$opts = "youtube-music-history=$History,youtube-music-plays=$Plays,youtube-music-seed=$id"
if ($Queue) {
    # плейлист понравившихся: очередь (id <TAB> название, первым — стартовый трек) youtube-music.lua
    # прочитает при старте; миксы он в этом режиме не подгружает
    $lines = [string[]]@($Queue | ForEach-Object { "$($_.Id)`t$($_.Title)" })
    [IO.File]::WriteAllLines($QueueFile, $lines, (New-Object Text.UTF8Encoding $false))
    $opts += ",youtube-music-queue=$QueueFile,youtube-music-source=$QueueName"
}
$mpvArgs = @(
    '--no-config'
    "--include=`"$Dir\youtube-music.conf`""
    "--script=`"$Dir\youtube-music.lua`""
    "--input-ipc-server=\\.\pipe\$Pipe"
    "--script-opts=`"$opts`""
)
if ($Queue) { $mpvArgs += '--loop-playlist=inf' }               # доиграли список — снова с начала
# глобальный повтор переживает смену радио; ← в music liked — повтор только на этот запуск
if ((Test-Path $RepeatFile) -or $RepeatOne) { $mpvArgs += '--loop-file=inf' }
$mpvArgs += "--volume=$(Get-SavedVolume)"                        # и громкость тоже (после --include — перекрывает конфиг)
if ($track) {
    # прямой поток; название задаём только этому файлу (группа --{ ... --})
    if ($track.Ua) { $mpvArgs += "--user-agent=`"$($track.Ua -replace '"', '')`"" }
    $mpvArgs += '--{', "--force-media-title=`"$($track.Title -replace '"', "''")`"", "`"$($track.Stream)`"", '--}'
} else {
    # не получилось заранее — пусть mpv сам спросит yt-dlp
    $mpvArgs += "ytdl://https://music.youtube.com/watch?v=$id"
}

# старое радио доигрывает, пока новое готовилось; выключаем его только теперь
Stop-Radio

# журнал проигранных растёт с каждым треком: изредка обрезаем до последних 300 строк (старый mpv уже не пишет)
if ((Test-Path $Plays) -and (Get-Item $Plays).Length -gt 100KB) {
    $keep = [string[]]([IO.File]::ReadAllLines($Plays, [Text.Encoding]::UTF8) | Select-Object -Last 300)
    [IO.File]::WriteAllLines($Plays, $keep, (New-Object Text.UTF8Encoding $false))
}

# именно mpv.exe: просто "mpv" находит обёртку mpv.com, которая висит лишним процессом
$MpvExe = (Get-Command mpv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1).Source
if (-not $MpvExe) { $MpvExe = 'mpv.exe' }
Start-Process $MpvExe -ArgumentList $mpvArgs -WindowStyle Hidden
exit 0
