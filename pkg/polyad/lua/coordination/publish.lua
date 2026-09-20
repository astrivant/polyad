-- KEYS: stream, deduplication key. ARGV: serialized resource identity.
if redis.call('EXISTS', KEYS[2]) == 1 then return false end

-- Append and record the short deduplication window atomically so concurrent watchers coalesce.
local id = redis.call('XADD', KEYS[1], '*', 'key', ARGV[1])
redis.call('SET', KEYS[2], id, 'EX', 5)
return id
