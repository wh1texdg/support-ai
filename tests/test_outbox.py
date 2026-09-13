from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database.models import Outbox
from worker import main as worker


async def test_delivery_retry_then_success(session, monkeypatch):
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(worker, "Session", factory)
    session.add(Outbox(telegram_id=42, content="Reply"))
    await session.commit()
    bot = AsyncMock()
    bot.send_message.side_effect = TimeoutError()
    assert await worker.deliver_one(bot)
    row = await session.scalar(select(Outbox))
    assert row.status == "pending" and row.attempts == 1
    from app.database.models import utcnow

    row.next_attempt_at = utcnow()
    await session.commit()
    bot.send_message.side_effect = None
    assert await worker.deliver_one(bot)
    await session.refresh(row)
    assert row.status == "sent" and row.attempts == 2
    assert not await worker.deliver_one(bot)


async def test_delivery_keeps_user_order(session, monkeypatch):
    from datetime import timedelta

    from app.database.models import utcnow

    monkeypatch.setattr(worker, "Session", async_sessionmaker(session.bind, expire_on_commit=False))
    session.add_all(
        [
            Outbox(telegram_id=42, content="first", next_attempt_at=utcnow() + timedelta(minutes=1)),
            Outbox(telegram_id=42, content="second"),
            Outbox(telegram_id=43, content="other user"),
        ]
    )
    await session.commit()
    bot = AsyncMock()
    assert await worker.deliver_one(bot)
    bot.send_message.assert_awaited_once_with(43, "other user", request_timeout=15)
    assert not await worker.deliver_one(bot)
