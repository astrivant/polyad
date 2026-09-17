-- KEYS: stream, snapshots. ARGV: graph identity. Return snapshot and current stream cursor.
local snapshot = redis.call('HGET', KEYS[2], ARGV[1])
if not snapshot then return false end
local last = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
return {snapshot, #last > 0 and last[1][1] or '0-0'}
