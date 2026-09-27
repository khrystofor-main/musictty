-- Бесконечное радио YouTube Music для mpv.
-- Когда в плейлисте остаётся мало треков, берёт микс (RDAMVM<id>) от текущего
-- трека через yt-dlp и дописывает в очередь только ещё не игравшие треки.
-- Уже проигранные записи удаляются из плейлиста, чтобы он не рос.
-- Следующий трек заранее превращается в прямую ссылку на аудиопоток,
-- поэтому переключение не ждёт yt-dlp.

local msg = require 'mp.msg'

local REFILL_WHEN_LEFT = 3   -- сколько треков должно оставаться в очереди
local MIX_SIZE = 25          -- сколько треков брать из одного микса
local KEEP_BEHIND = 30       -- сколько проигранных оставлять для "назад" (запись ≈ 1 КБ памяти)
local PREFETCH_AHEAD = 1     -- сколько следующих треков готовить заранее
local FORMAT = mp.get_property('ytdl-format', 'bestaudio')

local seen = {}
local fetching = false
local direct_id = {}         -- прямая ссылка на поток -> id трека
local resolving = {}         -- id -> true, пока идёт получение ссылки
local names = {}             -- id -> "Исполнитель — Название" (для music list)

-- плейлист понравившихся (music liked, → / ←): файл очереди "id <TAB> название", первым — стартовый трек.
-- В этом режиме миксы не подгружаем и проигранное не удаляем — mpv крутит очередь по кругу (--loop-playlist)
local queue_path = mp.get_opt('youtube-music-queue')
if queue_path == '' then queue_path = nil end

-- что играет, для music now: название плейлиста передаёт radio.ps1, без него — радио
local source = mp.get_opt('youtube-music-source')
if not source or source == '' then source = queue_path and 'playlist' or 'radio mix' end
mp.set_property('user-data/youtube-music/source', source)

-- история начальных треков: путь передаёт radio.ps1 через --script-opts
-- (плейлист понравившихся — не радио, в неё не пишем)
local history_path = mp.get_opt('youtube-music-history')
local seed_id = nil
local seed_recorded = history_path == nil or queue_path ~= nil

local function record_seed(label)
    if seed_recorded then return end
    seed_recorded = true
    local f = io.open(history_path, 'a')
    if not f then return end
    f:write(os.date('%Y-%m-%d %H:%M'), '\t', seed_id, '\t', label, '\n')
    f:close()
end

-- журнал всех проигранных треков для music history (переживает stop и смену радио)
local plays_path = mp.get_opt('youtube-music-plays')
local last_logged = nil

local function log_play(id, title)
    -- тот же трек подряд не пишем (повтор не даёт file-loaded, но на всякий случай)
    if not plays_path or id == last_logged then return end
    last_logged = id
    local f = io.open(plays_path, 'a')
    if not f then return end
    f:write(os.date('%Y-%m-%d %H:%M'), '\t', id, '\t', title or id, '\n')
    f:close()
end

local function video_id(url)
    if not url then return nil end
    return direct_id[url] or url:match('[?&]v=([%w_-]+)') or url:match('youtu%.be/([%w_-]+)')
end

local function track_url(id)
    -- ytdl:// сразу отдаёт адрес yt-dlp, без попытки открыть HTML-страницу самим mpv
    return 'ytdl://https://music.youtube.com/watch?v=' .. id
end

-- значение для списка опций loadfile: %длина%текст — запятые и '=' внутри не ломают разбор
local function quote_opt(s)
    return '%' .. #s .. '%' .. s
end

local function find_entry(filename)
    local count = mp.get_property_number('playlist-count', 0)
    for i = 0, count - 1 do
        if mp.get_property('playlist/' .. i .. '/filename') == filename then return i end
    end
    return nil
end

-- какие записи очереди уже звучали: entry id -> true. Нужно, чтобы после отката назад
-- music list показывал и треки, прослушанные "впереди" текущего
local played = {}

local function entry_id(i)
    return mp.get_property_number('playlist/' .. i .. '/id', -1)
end

-- заменить запись очереди на месте (прямая ссылка или обратно ytdl://), не теряя отметку "звучал"
local function replace_entry(idx, url, opts)
    local was_played = played[entry_id(idx)]
    if opts then mp.commandv('loadfile', url, 'insert-at', tostring(idx), opts)
    else mp.commandv('loadfile', url, 'insert-at', tostring(idx)) end
    mp.commandv('playlist-remove', tostring(idx + 1))
    if was_played then played[entry_id(idx)] = true end
end

-- список для music list: все прослушанные записи и текущая, в порядке очереди.
-- Кладём в user-data/youtube-music/list, radio.ps1 читает по IPC.
-- entry — постоянный номер записи в очереди mpv (не сдвигается, когда удаляются старые записи)
local function publish_list()
    local pos = mp.get_property_number('playlist-pos', -1)
    if pos < 0 then return end
    local count = mp.get_property_number('playlist-count', 0)
    local items = {}
    for i = 0, count - 1 do
        local eid = entry_id(i)
        if i <= pos or played[eid] then
            local id = video_id(mp.get_property('playlist/' .. i .. '/filename'))
            items[#items + 1] = {
                entry = eid,
                id = id or '',
                title = (id and names[id]) or (i == pos and mp.get_property('media-title')) or id or '?',
                current = (i == pos),
            }
        end
    end
    mp.set_property_native('user-data/youtube-music/list', items)
end

-- music list, стрелка влево: вернуться к записи, очередь после неё не трогаем
mp.register_script_message('youtube-music-jump', function(entry)
    entry = tonumber(entry)
    local count = mp.get_property_number('playlist-count', 0)
    for i = 0, count - 1 do
        if mp.get_property_number('playlist/' .. i .. '/id') == entry then
            mp.commandv('playlist-play-index', tostring(i))
            return
        end
    end
end)

-- YouTube изредка отдаёт ссылку, которая сразу отвечает 403. Проверяем первый килобайт
-- встроенным в Windows curl (доли секунды), чтобы не споткнуться о неё при переключении.
local function check_url(url, ua, cb)
    local args = { 'curl', '-s', '-o', 'NUL', '-r', '0-1023', '-m', '5', '-w', '%{http_code}' }
    if ua and ua ~= '' then table.insert(args, '-A'); table.insert(args, ua) end
    table.insert(args, url)
    mp.command_native_async({ name = 'subprocess', playback_only = false, capture_stdout = true, args = args },
        function(ok, res)
            local code = ok and res.stdout and res.stdout:match('%d%d%d') or '?'
            -- curl не нашёлся или сеть моргнула — не мешаем, пусть решает запасной путь при воспроизведении
            cb(code == '200' or code == '206' or not ok or res.status ~= 0, code)
        end)
end

-- заменить запись очереди на прямую ссылку, полученную заранее
local prefetch
prefetch = function(index, attempt)
    attempt = attempt or 1
    local filename = mp.get_property('playlist/' .. index .. '/filename')
    if not filename or not filename:find('^ytdl://') then return end
    local id = video_id(filename)
    if not id or (resolving[id] and attempt == 1) then return end
    resolving[id] = true
    mp.command_native_async({
        name = 'subprocess',
        playback_only = false,
        capture_stdout = true,
        capture_stderr = true,
        args = { 'yt-dlp', '--quiet', '--no-warnings', '--no-playlist', '--encoding', 'utf-8',
                 '-f', FORMAT,
                 '--print', '%(uploader,channel|)s\t%(title)s\t%(http_headers.User-Agent|)s',
                 '--print', 'urls',
                 'https://music.youtube.com/watch?v=' .. id },
    }, function(ok, res)
        if not ok or res.status ~= 0 then resolving[id] = nil; return end   -- останется ytdl:// и сработает как обычно
        local info, url = res.stdout:match('^([^\r\n]*)\r?\n([^\r\n]+)')
        if not url then resolving[id] = nil; return end
        local artist, title, ua = info:match('^([^\t]*)\t([^\t]*)\t(.*)$')
        check_url(url, ua, function(good, code)
            if not good then
                msg.warn('Ссылка ответила ' .. code .. ', попытка ' .. attempt .. ': ' .. id)
                if attempt < 2 then
                    local idx = find_entry(filename)
                    if idx then return prefetch(idx, attempt + 1) end
                end
                resolving[id] = nil
                return   -- останется ytdl:// — откроется обычным путём
            end
            resolving[id] = nil
            -- пока ждали, пользователь мог перелистнуть — ищем запись заново
            local idx = find_entry(filename)
            if not idx or idx == mp.get_property_number('playlist-pos', -1) then return end
            local name = (artist and artist ~= '') and (artist .. ' — ' .. title) or (title or id)
            names[id] = names[id] or name
            local opts = 'force-media-title=' .. quote_opt(name)
            if ua and ua ~= '' then opts = opts .. ',user-agent=' .. quote_opt(ua) end
            direct_id[url] = id
            replace_entry(idx, url, opts)
            msg.info('Готов заранее: ' .. name)
        end)
    end)
end

local function prefetch_next()
    local pos = mp.get_property_number('playlist-pos', -1)
    local count = mp.get_property_number('playlist-count', 0)
    for i = pos + 1, math.min(pos + PREFETCH_AHEAD, count - 1) do prefetch(i) end
end

local function refill(seed)
    if fetching or not seed then return end
    fetching = true
    local mix = 'https://music.youtube.com/watch?v=' .. seed .. '&list=RDAMVM' .. seed
    msg.info('Подгружаю микс от ' .. seed)
    mp.command_native_async({
        name = 'subprocess',
        playback_only = false,
        capture_stdout = true,
        capture_stderr = true,
        args = { 'yt-dlp', '--flat-playlist', '--quiet', '--no-warnings', '--encoding', 'utf-8',
                 '--playlist-end', tostring(MIX_SIZE),
                 '--print', '%(id)s\t%(uploader,channel|)s\t%(title)s', mix },
    }, function(ok, res)
        fetching = false
        if not ok or res.status ~= 0 then
            msg.warn('Не удалось получить микс: ' .. ((res and res.stderr) or '?'))
            return
        end
        local added = 0
        for line in res.stdout:gmatch('[^\r\n]+') do
            local id, artist, title = line:match('^([%w_-]+)\t([^\t]*)\t(.*)$')
            -- данные микса точнее всего (есть исполнитель) — перезаписываем название из плеера
            if id then
                names[id] = artist ~= '' and (artist .. ' — ' .. title) or title
            end
            -- первый трек микса — сам начальный трек, у него есть исполнитель
            if id and id == seed_id and not seed_recorded then
                record_seed(artist ~= '' and (artist .. ' — ' .. title) or title)
            end
            if id and #id == 11 and not seen[id] then
                seen[id] = true
                mp.commandv('loadfile', track_url(id), 'append-play')
                added = added + 1
            end
        end
        msg.info('Добавлено треков: ' .. added)
        -- начального трека в миксе не оказалось — берём хотя бы название из плеера
        if seed_id and not seed_recorded then
            record_seed(mp.get_property('media-title', seed_id))
        end
        -- Всё из микса уже играло — пробуем зацепиться за последний трек очереди
        if added == 0 then
            local last = mp.get_property_number('playlist-count', 1) - 1
            local other = video_id(mp.get_property('playlist/' .. last .. '/filename'))
            if other and other ~= seed then refill(other) end
        end
        prefetch_next()
        publish_list()
    end)
end

-- плейлист понравившихся: дописываем в очередь всё после стартового трека (он уже играет)
local function load_queue()
    local f = io.open(queue_path, 'r')
    if not f then return end
    local first = true
    for line in f:lines() do
        local id, title = line:match('^([%w_-]+)\t(.-)\r?$')
        if id then
            names[id] = title
            if first then first = false
            else mp.commandv('loadfile', track_url(id), 'append') end
        end
    end
    f:close()
    prefetch_next()
    publish_list()
end

-- очередь начинаем подгружать сразу при старте, не дожидаясь, пока загрузится первый трек
mp.register_event('start-file', function()
    if seed_id then return end
    local path = mp.get_property('path')
    -- radio.ps1 может запустить mpv сразу с прямой ссылкой на поток и передать id отдельно
    local opt_seed = mp.get_opt('youtube-music-seed')
    if opt_seed and opt_seed ~= '' then direct_id[path] = opt_seed end
    seed_id = video_id(path)
    if queue_path then
        load_queue()
    elseif seed_id then
        seen[seed_id] = true
        refill(seed_id)
    end
end)

mp.register_event('file-loaded', function()
    local pos = mp.get_property_number('playlist-pos', 0)
    played[entry_id(pos)] = true
    local id = video_id(mp.get_property('path'))
    if id then seen[id] = true end

    -- в радио чистим хвост уже проигранного и подгружаем микс; плейлист понравившихся не трогаем
    if not queue_path then
        for _ = 1, pos - KEEP_BEHIND do
            mp.commandv('playlist-remove', 0)
        end

        pos = mp.get_property_number('playlist-pos', 0)
        local left = mp.get_property_number('playlist-count', 1) - pos - 1
        if left < REFILL_WHEN_LEFT then refill(id) end
    end
    prefetch_next()
    if id and not names[id] then names[id] = mp.get_property('media-title') end
    if id then log_play(id, names[id]) end
    publish_list()
end)

local current_path = nil
mp.register_event('start-file', function() current_path = mp.get_property('path') end)

-- если трек не открылся (удалён, недоступен в регионе, ссылка устарела) — не даём радио заглохнуть
mp.register_event('end-file', function(e)
    if e.reason == 'error' then
        -- прямая ссылка устарела (живёт ~6 ч — актуально при "назад") или отклонена YouTube:
        -- меняем запись на месте на обычную через yt-dlp и играем её. Повторно не зациклится:
        -- ytdl:// сюда уже не попадает.
        local id = video_id(current_path)
        local idx = current_path and find_entry(current_path)
        if idx and id and not current_path:find('^ytdl://') then
            msg.warn('Прямая ссылка не открылась, пробую через yt-dlp: ' .. id)
            local fixed = track_url(id)
            replace_entry(idx, fixed)
            -- после ошибки mpv сам шагает на следующий трек и перебил бы команду,
            -- поэтому возвращаемся на исправленную запись уже после его шага
            mp.add_timeout(0.05, function()
                local at = find_entry(fixed)
                if at and at ~= mp.get_property_number('playlist-pos', -1) then
                    mp.commandv('playlist-play-index', tostring(at))
                end
            end)
            return
        end
        local pos = mp.get_property_number('playlist-pos', -1)
        local count = mp.get_property_number('playlist-count', 0)
        -- в плейлисте понравившихся конец очереди не страшен: --loop-playlist вернёт в начало
        if not queue_path and (pos < 0 or pos >= count - 1) then
            local last = mp.get_property('playlist/' .. (count - 1) .. '/filename')
            refill(video_id(last))
        end
    end
end)
