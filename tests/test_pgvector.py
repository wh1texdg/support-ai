"""Real retrieval and locking tests; TEST_DATABASE_URL must target a dedicated test DB."""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.database.models import Base, KnowledgeChunk, KnowledgeDocument
from app.services.rag import RAGService

pytestmark = pytest.mark.integration


async def test_real_pgvector_cosine_order():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for real PostgreSQL/pgvector test")
    schema = "test_" + uuid.uuid4().hex
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as session, session.begin():
            await session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            doc = KnowledgeDocument(title="Delivery", content="Shipping", source="test")
            session.add(doc)
            await session.flush()
            for content, vector in [("match", [1.0, 0.0]), ("orthogonal", [0.0, 1.0])]:
                session.add(
                    KnowledgeChunk(
                        document_id=doc.id,
                        content=content,
                        embedding=vector + [0.0] * 1534,
                        embedding_model=settings().embedding_model,
                    )
                )
            await session.flush()
            rag = RAGService(SimpleNamespace(create_embedding=AsyncMock(return_value=[1.0] + [0.0] * 1535)))
            hits = await rag.search(session, "Shipping")
            assert hits[0]["content"] == "match" and hits[0]["score"] == pytest.approx(1.0)
            assert hits[1]["score"] == pytest.approx(0.0)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
