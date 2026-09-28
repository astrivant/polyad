-- A known deferred decision must not starve its own dependency deliveries.
-- Move it behind queued siblings atomically, retaining at-least-once delivery.
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #pending == 0 or pending[1][2] ~= ARGV[3] then
    return false
end

-- Append first: if capacity rejects XADD, the original remains pending.
local replacement = redis.call('XADD', KEYS[1], '*', 'key', ARGV[4])
redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
redis.call('XDEL', KEYS[1], ARGV[2])
return replacement
