-- KEYS: stream, snapshots. ARGV: graph identity, snapshot JSON, revision, event JSON, retention.
local previous = redis.call('HGET', KEYS[2], ARGV[1])
local changed = not previous or cjson.decode(previous).revision ~= ARGV[3]
if not previous and redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[5]) then
    redis.call('DEL', KEYS[2])
end
local id = false
if changed then
    id = redis.call('XADD', KEYS[1], 'MAXLEN', ARGV[5], '*', 'event', ARGV[4])
end
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], 86400)
return id
