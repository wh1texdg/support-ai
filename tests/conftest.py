import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.database.models import Base
from app.database.session import get_session
from app.main import app
from app.services.llm import Answer
from app.services.rag import RAGService


@pytest.fixture
async def session():
    url = os.getenv("TEST_DATABASE_URL")
    schema = "test_" + uuid.uuid4().hex
    if url:
        engine = create_async_engine(
            url, connect_args={"server_settings": {"search_path": f"{schema},public"}}
        )
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    else:
        engine = create_async_engine("sqlite+aiosqlite://")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, record):
        if not url:
            connection.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        if url:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest.fixture
async def runtime():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    embeddings = SimpleNamespace(
        create_embeddings=AsyncMock(side_effect=lambda texts: [[1.0] + [0.0] * 1535 for _ in texts])
    )
    rag = RAGService(embeddings)
    rag.search = AsyncMock(return_value=[])
    llm = SimpleNamespace(
        generate_answer=AsyncMock(
            return_value=Answer(answer="Да, доставка доступна.", insufficient=False, source_ids=[1])
        )
    )
    yield SimpleNamespace(redis=redis, rag=rag, llm=llm)
    await redis.aclose()


@pytest.fixture
async def client(session, runtime, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin")
    monkeypatch.setenv("BOT_API_KEY", "test-bot")
    monkeypatch.setenv("OPERATOR_KEYS", '{"test-operator":"alice","other-operator":"bob"}')
    settings.cache_clear()

    async def override():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_session] = override
    app.state.runtime = runtime
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()
    settings.cache_clear()
