-- KEYS: rate hash, active leases. ARGV: rate limit, concurrency limit, lease ID, lease seconds.
local clock = redis.call('TIME')
local now = tonumber(clock[1])
local window = math.floor(now / 60)

-- One server clock and one atomic script keep every replica on the same rate window.
local count = 0
if tonumber(redis.call('HGET', KEYS[1], 'window')) == window then
  count = tonumber(redis.call('HGET', KEYS[1], 'count')) or 0
end

-- Reclaim expired leases before testing either budget; rejected requests consume neither.
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', now)
if count >= tonumber(ARGV[1]) then return {0, 60 - now % 60} end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[2]) then return {0, 1} end

-- Commit rate consumption and concurrent ownership together, after both checks succeed.
redis.call('HSET', KEYS[1], 'window', window, 'count', count + 1)
redis.call('EXPIRE', KEYS[1], 120)
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[4]), ARGV[3])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]) * 2)
return {1, 0}
