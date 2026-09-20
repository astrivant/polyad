-- KEYS: active leases. ARGV: lease ID, lease seconds.
local now = tonumber(redis.call('TIME')[1])
local deadline = tonumber(redis.call('ZSCORE', KEYS[1], ARGV[1]))

-- An expired permit cannot be revived; its caller must stop work and acquire a new one.
if not deadline or deadline <= now then return 0 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]) * 2)
return 1
