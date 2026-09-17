local cooldown = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= burst then
    return math.max(1, redis.call('PTTL', KEYS[1]))
end
local next_count = redis.call('INCR', KEYS[1])
if next_count == 1 then redis.call('PEXPIRE', KEYS[1], cooldown) end
return 0
