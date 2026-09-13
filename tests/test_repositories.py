import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.database.models import FAQ, Product, User
from app.database.repositories import listing, require
from app.scripts.seed import seed


async def test_repository_lookup_pagination(session):
    session.add_all([User(telegram_id=1), User(telegram_id=2)])
    await session.flush()
    rows = await listing(session, User, limit=1)
    assert len(rows) == 1 and rows[0].telegram_id == 2
    assert (await require(session, User, rows[0].id)).telegram_id == 2
    with pytest.raises(HTTPException) as error:
        await require(session, User, 999)
    assert error.value.status_code == 404


async def test_seed_idempotent(session):
    await seed(session)
    await session.flush()
    await seed(session)
    await session.flush()
    assert await session.scalar(select(func.count()).select_from(Product)) == 20
    assert await session.scalar(select(func.count()).select_from(FAQ)) == 24
