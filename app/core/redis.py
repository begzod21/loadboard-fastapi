import redis.asyncio as redis
from app.core.config import settings

redis_client = redis.from_url(
    settings.redis.redis_url,
    decode_responses=True,
)