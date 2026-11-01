-- Keep arrow seeking precise for project launches; e.g. replay the configured step.
local mp = require 'mp'
local opts = {seconds = 0}
require('mp.options').read_options(opts, 'ytw_seek')
-- Python owns defaults and validation; require its launch-specific value here.
assert(opts.seconds > 0 and opts.seconds < math.huge, 'project launcher must supply the seek step')

for key, direction in pairs({LEFT = -1, RIGHT = 1}) do
    -- Exact seeking decodes to the target instead of snapping to a keyframe.
    mp.add_forced_key_binding(key, 'seek-' .. key, function()
        mp.commandv('seek', direction * opts.seconds, 'relative+exact')
    end, {repeatable = true})
end
