-- KEYS: stream, resource versions. ARGV: resource UID, resource version, event JSON, retention.
if redis.call('HGET', KEYS[2], ARGV[1]) == ARGV[2] then return false end
if redis.call('HLEN', KEYS[2]) >= tonumber(ARGV[4]) and redis.call('HEXISTS', KEYS[2], ARGV[1]) == 0 then
    redis.call('DEL', KEYS[2])
end
local id = redis.call('XADD', KEYS[1], 'MAXLEN', ARGV[4], '*', 'event', ARGV[3])
redis.call('HSET', KEYS[2], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[2], 86400)
return id
