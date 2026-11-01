-- Keep arrow seeking precise for project launches; e.g. replay the last 1.5 seconds.
local mp = require 'mp'

for key, seconds in pairs({LEFT = -1.5, RIGHT = 1.5}) do
    -- Exact seeking decodes to the target instead of snapping to a keyframe.
    mp.add_forced_key_binding(key, 'seek-' .. key, function()
        mp.commandv('seek', seconds, 'relative+exact')
    end, {repeatable = true})
end
