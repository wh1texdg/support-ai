from fastapi import HTTPException
from sqlalchemy import select


async def require(session, model, identity, *, lock=False):
    query = select(model).where(model.id == identity)
    if lock:
        query = query.with_for_update()
    row = await session.scalar(query)
    if row is None:
        raise HTTPException(404, "Not found")
    return row


async def listing(session, model, offset=0, limit=50):
    return list(
        (await session.scalars(select(model).order_by(model.id.desc()).offset(offset).limit(limit))).all()
    )


def serialize(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}
