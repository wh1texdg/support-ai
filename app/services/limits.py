from fastapi import HTTPException
from redis.exceptions import RedisError

SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return n
"""


async def rate_limit(redis, telegram_id):
    try:
        count = await redis.eval(SCRIPT, 1, f"rate:{telegram_id}")
    except RedisError as exc:
        # Fail closed for paid AI calls; explicit handoff does not use Redis.
        raise HTTPException(503, "AI временно недоступен. Используйте /operator.") from exc
    if count > 10:
        raise HTTPException(
            429, "Не более 10 запросов в минуту. Попробуйте через минуту.", headers={"Retry-After": "60"}
        )
