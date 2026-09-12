import redis.asyncio as redis

from app.core.config import settings

redis_client = redis.from_url(
    settings.redis.redis_url,
    decode_responses=True,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    socket_connect_timeout=2,
    socket_timeout=2,
)


async def close_redis() -> None:
    await redis_client.aclose()