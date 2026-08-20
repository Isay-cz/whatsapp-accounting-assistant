from redis.asyncio import Redis

from config import Settings


def get_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)
