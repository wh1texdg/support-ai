import hashlib
import json
import logging
import math

from redis.exceptions import RedisError

from app.core.config import settings

log = logging.getLogger(__name__)
DIMENSIONS = 1536


class EmbeddingService:
    def __init__(self, client, redis):
        self.client, self.redis = client, redis

    async def create_embeddings(self, texts):
        if not texts:
            return []
        vectors = []
        for start in range(0, len(texts), 64):
            batch = texts[start : start + 64]
            result = await self.client.embeddings.create(
                model=settings().embedding_model, input=batch, dimensions=DIMENSIONS
            )
            data = sorted(result.data, key=lambda item: item.index)
            if len(data) != len(batch):
                raise ValueError("Wrong embedding count")
            for item in data:
                vector = item.embedding
                if len(vector) != DIMENSIONS or not all(math.isfinite(x) for x in vector):
                    raise ValueError("Invalid embedding")
                if not any(vector):
                    raise ValueError("Zero embedding")
                vectors.append(vector)
        return vectors

    async def create_embedding(self, text):
        key = "embedding:" + settings().embedding_model + ":" + hashlib.sha256(text.encode()).hexdigest()
        try:
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
        except (RedisError, ValueError):
            log.warning("embedding_cache_unavailable")
        vector = (await self.create_embeddings([text]))[0]
        try:
            await self.redis.set(key, json.dumps(vector), ex=3600)
        except RedisError:
            log.warning("embedding_cache_write_failed")
        return vector
