-- Render selectable subtitle words inside mpv; e.g. click a Dutch word to save it.
local mp = require 'mp'
local utils = require 'mp.utils'
local overlay = mp.create_osd_overlay('ass-events')
local measure = mp.create_osd_overlay('ass-events')
measure.hidden, measure.compute_bounds = true, true
local config, tracks, requests, marks, metrics = nil, {}, {}, {}, {}
local enabled, bound, pending_render = true, false, false
local boxes, hovered, serial, last_request = {}, nil, 0, 0
local message, message_until = '', 0
local routes = {
    {prefix = '', sid = 'sid', visible = 'sub-visibility', wanted = true},
    {prefix = 'secondary-', sid = 'secondary-sid', visible = 'secondary-sub-visibility', wanted = true},
}
local render, schedule, update_hover

-- Keep text literal, including braces; e.g. subtitle text cannot inject ASS commands.
local function escape(text)
    return mp.command_native({'escape-ass', text})
end

-- Normalize only presentation whitespace; e.g. ASS replaces source line breaks with spaces.
local function plain(text)
    return (text or ''):gsub('%s+', ' '):gsub('^ ', ''):gsub(' $', '')
end

-- Compare external track paths on Windows; e.g. the generated secondary ASS path.
local function path_key(path)
    return (path or ''):gsub('\\', '/'):lower()
end

-- Convert RGB into ASS BGR; e.g. retain the user's cyan and yellow subtitles.
local function color(css)
    local hex = (css or '#FFFFFF'):gsub('#', '')
    return hex:sub(5,6) .. hex:sub(3,4) .. hex:sub(1,2)
end

-- Give every displayed occurrence a stable local key; e.g. revisiting a cue restores green text.
local function mark_key(track, cue, token)
    return track .. ':' .. cue .. ':' .. token
end

-- Reuse the same ASS geometry for drawing and measuring; e.g. no guessed character widths.
local function event(box, tint, underline)
    return string.format('{\\an7\\q2\\fn%s\\fs%.3f\\bord1.6\\shad0\\pos(%.3f,%.3f)\\1c&H%s&\\1a&H%s&\\u%d}%s',
        box.font, box.size, box.x, box.y, tint or box.tint, box.alpha or '00',
        underline and 1 or 0, escape(box.display))
end

-- Cache expensive renderer measurements until size/font changes; e.g. no per-frame measuring.
local function dimensions(text, size, font, w, h)
    local key = text .. '\0' .. size .. '\0' .. font .. '\0' .. w .. ':' .. h
    if not metrics[key] then
        measure.res_x, measure.res_y = w, h
        measure.data = event({display = text, x = 20, y = 20, size = size, font = font, tint = 'FFFFFF'})
        local b = measure:update()
        if not b or not b.x0 or not b.x1 then return nil end
        metrics[key] = {width = b.x1 - b.x0, dx = b.x0 - 20,
                       dy = b.y0 - 20, height = b.y1 - b.y0}
    end
    return metrics[key]
end

-- Follow mpv's selected track rather than assuming GUI focus; e.g. cycle subtitle tracks.
local function selected_track(route)
    local sid = mp.get_property_number(route.sid, -1)
    for _, track in ipairs(mp.get_property_native('track-list', {})) do
        if track.type == 'sub' and track.id == sid then
            local filename = path_key(track['external-filename'])
            for idx, known in ipairs(tracks) do
                if filename == path_key(known.path) then return idx, known end
            end
        end
    end
end

-- Let mpv own subtitle timing and delay; e.g. find the source cue it is displaying now.
local function current_cue(route, track)
    local text = plain(mp.get_property(route.prefix .. 'sub-text', ''))
    if text == '' then return nil end
    local start = mp.get_property_number(route.prefix .. 'sub-start/full', nil)
        or mp.get_property_number(route.prefix .. 'sub-start', 0)
    local found, distance = nil, math.huge
    for idx, cue in ipairs(track.cues) do
        local delta = math.abs(cue.start - start)
        if plain(cue.text) == text and delta < distance then found, distance = idx, delta end
    end
    return found
end

-- Hide only a track we can render; e.g. unsupported embedded ASS remains native.
local function takeover(route, active)
    if active and not route.owned then
        route.wanted = mp.get_property_bool(route.visible, true)
        route.owned = true
        mp.set_property_bool(route.visible, false)
    elseif not active and route.owned then
        route.owned = false
        mp.set_property_bool(route.visible, route.wanted)
    end
    local binding = route.prefix .. 'vocab-visible'
    if active then
        mp.add_forced_key_binding(route.prefix == '' and 'v' or 'Alt+v', binding, function()
            route.wanted = not route.wanted
            schedule()
        end)
    else
        mp.remove_key_binding(binding)
    end
end

-- Lay out one cue in centered lines using measured words; e.g. preserve explicit SRT breaks.
local function layout(track_idx, cue_idx, w, h)
    local track, cue = tracks[track_idx], tracks[track_idx].cues[cue_idx]
    local style = track.style
    local position = style.res_y == 720 and mp.get_property_number('sub-pos', style.position) or style.position
    local alpha = #style.color == 9 and string.format('%02X', 255 - tonumber(style.color:sub(8,9), 16)) or '00'
    local font_size = style.res_y == 720 and mp.get_property_number('sub-font-size', style.font_size) or style.font_size
    local size = font_size * h / style.res_y * mp.get_property_number('sub-scale', 1)
    local font = style.res_y == 1080 and 'Segoe UI' or mp.get_property('sub-font', 'sans-serif')
    local gap, line_height, max_width = size * 0.26, size * 1.4, w * 0.90
    local lines, line = {}, {width = 0, words = {}}
    for idx, token in ipairs(cue.tokens) do
        local d = dimensions(token.display, size, font, w, h)
        if not d then return nil end
        local hard_break = token.line and #line.words > 0 and token.line ~= line.line
        if #line.words > 0 and (hard_break or line.width + gap + d.width > max_width) then
            lines[#lines + 1] = line
            line = {width = 0, words = {}}
        end
        local offset = #line.words > 0 and gap or 0
        line.words[#line.words + 1] = {idx = idx, token = token, d = d, x = line.width + offset}
        line.width, line.line = line.width + offset + d.width, token.line
    end
    if #line.words > 0 then lines[#lines + 1] = line end
    local margin = h * 0.045
    local y = h * position / 100
    if position >= 67 then y = y - margin - #lines * line_height
    elseif position > 33 then y = y - #lines * line_height / 2 end
    y = math.max(margin, math.min(y, h - margin - #lines * line_height))
    local result = {}
    for _, row in ipairs(lines) do
        local left = (w - row.width) / 2
        for _, word in ipairs(row.words) do
            local x, d = left + word.x, word.d
            result[#result + 1] = {track = track_idx, cue = cue_idx, token = word.idx,
                x = x - d.dx, y = y, size = size, font = font, tint = color(style.color),
                display = word.token.display, alpha = alpha, clickable = word.token['end'] > word.token.start,
                x0 = x - 2, x1 = x + d.width + 2,
                y0 = y + d.dy - 3, y1 = y + d.dy + d.height + 4}
        end
        y = y + line_height
    end
    return result
end

-- Display feedback within the same mpv surface; e.g. an API failure never removes a saved mark.
local function toast(text)
    message, message_until = text, mp.get_time() + 6
    schedule()
    mp.add_timeout(6.1, schedule)
end

-- Capture mouse-down once, including double-clicks; e.g. two intentional clicks add two entries.
local function click(e)
    if e and e.event ~= 'down' and e.event ~= 'press' then return end
    update_hover()
    if not hovered or not config then return end
    serial, last_request = serial + 1, serial + 1
    local key = mark_key(hovered.track, hovered.cue, hovered.token)
    requests[serial] = {key = key, display = hovered.display}
    mp.commandv('script-message', 'vocab-click', utils.format_json({request = serial,
        track = hovered.track, cue = hovered.cue, token = hovered.token,
        position = mp.get_property_number('time-pos', 0)}))
    toast('Saving “' .. hovered.display .. '”…')
end

-- Bind the mouse only over a word; e.g. the normal seekbar and window dragging keep working.
update_hover = function()
    local mouse = mp.get_property_native('mouse-pos', {})
    local hit = nil
    if enabled and mouse.hover ~= false and mouse.x and mouse.y then
        for _, box in ipairs(boxes) do
            if box.clickable and mouse.x >= box.x0 and mouse.x < box.x1
                and mouse.y >= box.y0 and mouse.y < box.y1 then hit = box; break end
        end
    end
    local changed = (hovered and mark_key(hovered.track, hovered.cue, hovered.token))
        ~= (hit and mark_key(hit.track, hit.cue, hit.token))
    hovered = hit
    if hit and not bound then
        bound = true
        mp.add_forced_key_binding('MBTN_LEFT', 'vocab-click-word', click, {complex = true})
        mp.add_forced_key_binding('MBTN_LEFT_DBL', 'vocab-double-word', click, {complex = true})
    elseif not hit and bound then
        bound = false
        mp.remove_key_binding('vocab-click-word')
        mp.remove_key_binding('vocab-double-word')
    end
    if changed then schedule() end
end

-- Draw the supported selected tracks and expose hitboxes for inspection.
render = function()
    pending_render = false
    local w, h = mp.get_osd_size()
    if not config or w < 10 or h < 10 then return end
    local events = {}
    boxes = {}
    overlay.res_x, overlay.res_y = w, h
    for _, route in ipairs(routes) do
        local idx, track = selected_track(route)
        local cue_idx = track and current_cue(route, track)
        local key = cue_idx and (idx .. ':' .. cue_idx .. ':' .. w .. ':' .. h)
        if key and route.layout_key ~= key then
            route.layout = layout(idx, cue_idx, w, h)
            route.layout_key = key
        end
        local active = enabled and key ~= nil and route.layout ~= nil
        takeover(route, active)
        if active and route.wanted then
            for _, box in ipairs(route.layout) do
                boxes[#boxes + 1] = box
                local key = mark_key(box.track, box.cue, box.token)
                local saved = marks[key]
                box.saved = saved or false
                local hover = hovered and key == mark_key(hovered.track, hovered.cue, hovered.token)
                local tint = saved and '8FE79A' or (hover and 'FFFFFF' or box.tint)
                events[#events + 1] = event(box, tint, saved or hover)
            end
        end
    end
    if message_until > mp.get_time() then
        events[#events + 1] = string.format('{\\an8\\pos(%.1f,%.1f)\\fnSegoe UI\\fs%.1f\\bord1.5}%s',
            w / 2, h * 0.48, math.max(18, h * 0.04), escape(message))
    end
    overlay.data = table.concat(events, '\n')
    overlay:update()
    -- A namespaced inspectable property also supports genuine mpv integration tests.
    mp.set_property_native('user-data/yt-whisper-subs/word-boxes', boxes)
    update_hover()
end

-- Coalesce cue, track, and window changes; e.g. a resize computes one fresh layout.
schedule = function()
    if not pending_render then pending_render = true; mp.add_timeout(0, render) end
end

mp.register_script_message('vocab-config', function(document)
    if config then return end
    config = utils.parse_json(document)
    if not config then return end
    tracks = config.tracks
    for ti, track in ipairs(tracks) do
        for ci, cue in ipairs(track.cues) do
            for _, wi in ipairs(cue.saved or {}) do marks[mark_key(ti, ci, wi)] = true end
        end
    end
    schedule()
end)

mp.register_script_message('vocab-result', function(document)
    local result = utils.parse_json(document)
    local request = result and requests[result.request]
    if not request then return end
    local last = mp.get_property_native('user-data/yt-whisper-subs/last-save', {})
    if not last.request or result.request >= last.request then
        mp.set_property_native('user-data/yt-whisper-subs/last-save', result)
    end
    if result.saved then marks[request.key] = true end
    if result.request == last_request then
        if not result.saved then toast('Could not save word: ' .. (result.error or 'storage error'))
        elseif result.state == 'ready' then toast(request.display .. ' → ' .. (result.meaning or ''))
        elseif result.state == 'failed' then toast('Saved “' .. request.display .. '” · translation needs retry in Vocabulary')
        else
            toast(string.format('Saved “%s” · %s clicks / %s videos · translating…',
                request.display, result.clicks or '?', result.videos or '?'))
        end
    end
    if result.state == 'ready' or result.state == 'failed' or not result.saved then requests[result.request] = nil end
    schedule()
end)

mp.add_key_binding('Ctrl+Shift+v', 'vocab-toggle', function()
    enabled = not enabled
    toast(enabled and 'Clickable subtitles enabled' or 'Normal subtitles enabled')
end)

for _, name in ipairs({'sub-text', 'secondary-sub-text', 'sub-start/full', 'secondary-sub-start/full',
    'sid', 'secondary-sid', 'track-list', 'osd-dimensions', 'sub-font', 'sub-font-size', 'sub-scale', 'sub-pos'}) do
    mp.observe_property(name, 'native', function()
        if name == 'osd-dimensions' or name == 'sub-font' or name == 'sub-font-size' or name == 'sub-scale' or name == 'sub-pos' then
            metrics = {}
            for _, route in ipairs(routes) do route.layout_key = nil end
        end
        schedule()
    end)
end
mp.observe_property('mouse-pos', 'native', update_hover)
-- Mouse position changes are not observable on every VO; poll without stealing MOUSE_MOVE from OSC.
mp.add_periodic_timer(0.03, update_hover)
mp.register_event('shutdown', function()
    for _, route in ipairs(routes) do takeover(route, false) end
end)
mp.commandv('script-message', 'vocab-ready')
