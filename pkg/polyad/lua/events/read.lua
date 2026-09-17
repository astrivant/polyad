-- KEYS: stream. ARGV: last observed stream cursor, maximum entries per read.
local first = redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', 1)
local function older(a, b)
    local am, as = string.match(a, '^(%d+)%-(%d+)$')
    local bm, bs = string.match(b, '^(%d+)%-(%d+)$')
    if #am ~= #bm then return #am < #bm end
    if am ~= bm then return am < bm end
    if #as ~= #bs then return #as < #bs end
    return as < bs
end
if ARGV[1] ~= '0-0' and (#first == 0 or older(ARGV[1], first[1][1])) then
    return redis.error_reply('CURSOR_EXPIRED')
end
return redis.call('XRANGE', KEYS[1], '(' .. ARGV[1], '+', 'COUNT', tonumber(ARGV[2]) or 64)
