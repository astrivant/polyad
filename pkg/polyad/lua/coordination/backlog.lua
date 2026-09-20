-- KEYS: stream. ARGV: consumer group. Return total and pending counts.
local total = redis.call('XLEN', KEYS[1])
if total == 0 then return {0, 0} end

-- A not-yet-created consumer group has no claimed work, but its stream may already have a backlog.
local pending = redis.pcall('XPENDING', KEYS[1], ARGV[1])
if pending.err then
    if string.find(pending.err, 'NOGROUP', 1, true) then return {total, 0} end
    return redis.error_reply(pending.err)
end
return {total, pending[1]}
