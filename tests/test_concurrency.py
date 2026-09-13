import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.schemas import ChatIn
from app.database.models import Conversation, IncomingEvent, Message, SupportRequest
from app.services.chat import chat
from app.services.support import take

pytestmark = pytest.mark.integration


async def test_concurrent_duplicate_event_and_operator_claim(session, runtime):
    if session.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL locks require TEST_DATABASE_URL")
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    request = ChatIn(telegram_id=98765, event_id="concurrent-1", text="/operator")

    async def submit():
        async with factory() as transaction, transaction.begin():
            return await chat(transaction, runtime, request)

    first, second = await asyncio.gather(submit(), submit())
    assert first == second
    assert await session.scalar(select(func.count()).select_from(IncomingEvent)) == 1
    assert await session.scalar(select(func.count()).select_from(Message)) == 2
    assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
    support = await session.scalar(select(SupportRequest))

    async def claim(identity):
        try:
            async with factory() as transaction, transaction.begin():
                await take(transaction, support.id, identity)
            return "claimed"
        except HTTPException as exc:
            assert exc.status_code == 409
            return "conflict"

    assert sorted(await asyncio.gather(claim("alice"), claim("bob"))) == ["claimed", "conflict"]
