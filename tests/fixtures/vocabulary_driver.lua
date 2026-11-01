-- Exercise real mpv mouse/script events; e.g. opt-in vocabulary integration tests.
local mp = require 'mp'
local utils = require 'mp.utils'
local state, deadline = 0, mp.get_time() + 12
local pending_click = false

-- Report an observable player failure; e.g. counts disappear after an asynchronous translation.
local function fail(reason)
    mp.commandv('script-message', 'vocab-test-result', utils.format_json({state = state, error = reason}))
    mp.commandv('quit', 1)
end

-- Read the production renderer's inspectable hitboxes; e.g. after resize or seek.
local function box(cue, token)
    for _, item in ipairs(mp.get_property_native('user-data/yt-whisper-subs/word-boxes', {})) do
        if item.track == 2 and item.cue == cue and item.token == token then return item end
    end
end

-- Send a real input event through mpv, rather than calling the Python capture handler.
local function click(cue, token, mode)
    local item = box(cue, token)
    if not item or pending_click then return end
    pending_click = true
    local x, y = math.floor((item.x0 + item.x1) / 2), math.floor((item.y0 + item.y1) / 2)
    mp.commandv('keypress', 'MOUSE_ENTER')
    mp.commandv('mouse', x, y)
    mp.add_timeout(0.12, function()
        pending_click = false
        local current = box(cue, token)
        if not current then return end
        -- A fullscreen transition can still be resizing; move again before clicking its new hitbox.
        if current.x0 ~= item.x0 or current.y0 ~= item.y0 or current.x1 ~= item.x1 then
            click(cue, token, mode)
            return
        end
        mp.commandv('mouse', x, y, 0, mode or 'single')
    end)
end

-- Drive stages only after durable acknowledgements; e.g. no timing-only pass conditions.
mp.add_periodic_timer(0.1, function()
    local saved = mp.get_property_native('user-data/yt-whisper-subs/last-save', {})
    local feedback = mp.get_property_native('user-data/yt-whisper-subs/word-feedback', {})
    if mp.get_time() > deadline then
        mp.commandv('script-message', 'vocab-test-result', utils.format_json({state = state, timeout = true,
            mouse = mp.get_property_native('mouse-pos'), saved = saved,
            boxes = mp.get_property_native('user-data/yt-whisper-subs/word-boxes')}))
        mp.commandv('quit', 1)
    elseif state == 0 and box(1, 3) then
        click(1, 3)
        state = 1
    elseif state == 1 and saved.request == 1 and saved.saved then
        if feedback.text ~= '1/1 · ...' or feedback.meaning ~= '' then
            return fail('pending feedback must show compact counts and dots')
        end
        click(1, 3, 'double')
        state = 2
    elseif state == 2 and saved.request == 2 and saved.saved then
        if feedback.text ~= '2/1 · ...' then return fail('repeated clicks must update pending counts') end
        mp.commandv('script-message', 'vocab-test-release')
        state = 2.5
    elseif state == 2.5 and saved.request == 2 and saved.state == 'ready' then
        if feedback.text ~= '2/1' or feedback.meaning ~= 'interested'
            or feedback.note ~= 'Adjective describing interest.' then
            return fail('completed feedback must retain counts and separate translation from its note')
        end
        if os.getenv('YTW_MPV_PREVIEW') then
            mp.commandv('screenshot-to-file', os.getenv('YTW_MPV_PREVIEW'), 'window')
        end
        mp.commandv('mouse', 2, 2, 0, 'single')
        mp.set_property('geometry', '640x360')
        state = 3
    elseif state == 3 and select(1, mp.get_osd_size()) == 640 and box(1, 4) then
        click(1, 4)
        state = 4
    elseif state == 4 and saved.request == 3 and saved.saved then
        mp.commandv('seek', 7, 'absolute+exact')
        state = 5
    elseif state == 5 and box(2, 2) then
        click(2, 2)
        state = 6
    elseif state == 6 and saved.request == 4 and saved.saved then
        mp.commandv('keypress', 'Ctrl+Shift+v')
        state = 7
    elseif state == 7 then
        local boxes = mp.get_property_native('user-data/yt-whisper-subs/word-boxes', {})
        if #boxes == 0 and mp.get_property_bool('secondary-sub-visibility') then
            mp.commandv('keypress', 'Ctrl+Shift+v')
            mp.set_property_bool('fullscreen', true)
            mp.set_property_bool('window-minimized', true)
            state = 8
        end
    elseif state == 8 and mp.get_property_bool('fullscreen') and box(2, 2) then
        click(2, 2)
        state = 9
    elseif state == 9 and saved.request == 5 and saved.saved then
        mp.commandv('keypress', 'v')
        state = 10
    elseif state == 10 then
        local only_secondary = true
        for _, item in ipairs(mp.get_property_native('user-data/yt-whisper-subs/word-boxes', {})) do
            if item.track == 1 then only_secondary = false end
        end
        if only_secondary then
            mp.commandv('keypress', 'Alt+v')
            state = 11
        end
    elseif state == 11 then
        local boxes = mp.get_property_native('user-data/yt-whisper-subs/word-boxes', {})
        if #boxes == 0 then
            mp.set_property_bool('fullscreen', false)
            mp.commandv('script-message', 'vocab-test-result', utils.format_json({state = state, passed = true}))
            mp.commandv('quit', 0)
        end
    end
end)
