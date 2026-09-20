-- Atomically replace, withdraw and expire a bounded graph's replica contracts.
-- Redis time is the lease authority; callers cannot extend an offer by dating it ahead.
local key = KEYS[1]
local command, uid, payload, ttl = ARGV[1], ARGV[2], ARGV[3], tonumber(ARGV[4])
local clock = redis.call('TIME')
local now = tonumber(clock[1]) + tonumber(clock[2]) / 1000000
local entries = redis.call('HGETALL', key)
for index = 1, #entries, 2 do
    local entry = cjson.decode(entries[index + 1])
    if entry.expiresAt <= now then
        redis.call('HDEL', key, entries[index])
    end
end

if command == 'withdraw' then
    redis.call('HDEL', key, uid)
    return '{}'
end
if command == 'publish' then
    if redis.call('HEXISTS', key, uid) == 0 and redis.call('HLEN', key) >= 128 then
        return redis.error_reply('capability contract limit reached')
    end
    -- Keep application JSON opaque. Lua cjson's numeric precision must not
    -- round resource counters, concurrency limits or application work rates.
    local entry = {payload = payload, observedAt = now, expiresAt = now + ttl}
    local encoded = cjson.encode(entry)
    redis.call('HSET', key, uid, encoded)

    -- The whole hash can disappear after the maximum per-entry lifetime.
    -- Refreshing one replica never renews another replica's embedded expiry.
    redis.call('EXPIRE', key, 300)
    return encoded
end
return cjson.encode({observedAt = now, entries = redis.call('HVALS', key)})
