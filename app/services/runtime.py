from dataclasses import dataclass

from openai import AsyncOpenAI
from redis.asyncio import Redis

from app.core.config import settings
from app.services.embeddings import EmbeddingService
from app.services.llm import OpenAIProvider
from app.services.rag import RAGService


@dataclass
class Runtime:
    redis: Redis
    client: AsyncOpenAI
    rag: RAGService
    llm: OpenAIProvider

    async def close(self):
        await self.redis.aclose()
        await self.client.close()


def create_runtime():
    redis = Redis.from_url(
        settings().redis_url, decode_responses=True, socket_connect_timeout=2, socket_timeout=2
    )
    # Missing key permits startup and operator-only operation; remote requests fail gracefully.
    client = AsyncOpenAI(
        api_key=(
            settings().polza_ai_api_key.get_secret_value()
            or settings().openai_api_key.get_secret_value()
            or "unconfigured"
        ),
        base_url=settings().polza_base_url if settings().polza_ai_api_key.get_secret_value() else None,
        timeout=20,
        max_retries=1,
    )
    return Runtime(redis, client, RAGService(EmbeddingService(client, redis)), OpenAIProvider(client))
