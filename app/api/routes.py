from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import admin, bot_auth, operator, staff
from app.api.schemas import ChatIn, FAQIn, FAQPatch, FeedbackIn, KnowledgeIn, ProductIn, ProductPatch, TextIn
from app.database.models import (
    FAQ,
    Conversation,
    KnowledgeDocument,
    Message,
    Outbox,
    Product,
    SupportRequest,
    User,
)
from app.database.repositories import listing, require, serialize
from app.database.session import get_session
from app.services.chat import chat, feedback
from app.services.rag import indexing_lock
from app.services.support import operator_action, take

router = APIRouter(prefix="/api")
DB = Annotated[AsyncSession, Depends(get_session)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
Operator = Annotated[str, Depends(operator)]


def runtime(request: Request):
    return request.app.state.runtime


RT = Annotated[object, Depends(runtime)]


def catalog_routes(path, model, create_schema, patch_schema):
    catalog = APIRouter(prefix=path, dependencies=[Depends(admin)])

    @catalog.get("")
    async def all_rows(db: DB, offset: Offset = 0, limit: Limit = 50):
        return [serialize(row) for row in await listing(db, model, offset, limit)]

    @catalog.get("/{identity}")
    async def one_row(identity: int, db: DB):
        return serialize(await require(db, model, identity))

    async def create_row(data, db: DB, rt: RT):
        await indexing_lock(db)
        row = model(**data.model_dump())
        db.add(row)
        await db.flush()
        await rt.rag.sync_entity(db, row)
        return serialize(row)

    create_row.__annotations__["data"] = create_schema
    catalog.add_api_route("", create_row, methods=["POST"], status_code=201)

    async def patch_row(identity: int, data, db: DB, rt: RT):
        await indexing_lock(db)
        row = await require(db, model, identity, lock=True)
        changes = data.model_dump(exclude_unset=True)
        if any(value is None for value in changes.values()):
            raise HTTPException(422, "Fields cannot be null")
        for key, value in changes.items():
            setattr(row, key, value)
        await db.flush()
        await rt.rag.sync_entity(db, row)
        return serialize(row)

    patch_row.__annotations__["data"] = patch_schema
    catalog.add_api_route("/{identity}", patch_row, methods=["PATCH"])

    @catalog.delete("/{identity}", status_code=204)
    async def delete_row(identity: int, db: DB, rt: RT):
        await indexing_lock(db)
        row = await require(db, model, identity, lock=True)
        await rt.rag.delete_entity(db, row)
        await db.delete(row)

    return catalog


router.include_router(catalog_routes("/products", Product, ProductIn, ProductPatch))
router.include_router(catalog_routes("/faq", FAQ, FAQIn, FAQPatch))


@router.get("/knowledge", dependencies=[Depends(admin)])
async def knowledge(db: DB, offset: Offset = 0, limit: Limit = 50):
    return [serialize(row) for row in await listing(db, KnowledgeDocument, offset, limit)]


@router.post("/knowledge", status_code=201, dependencies=[Depends(admin)])
async def add_knowledge(data: KnowledgeIn, db: DB, rt: RT):
    if data.source.startswith(("product:", "faq:")):
        raise HTTPException(422, "Reserved source prefix")
    await indexing_lock(db)
    document = KnowledgeDocument(**data.model_dump())
    db.add(document)
    await db.flush()
    await rt.rag.index_document(db, document)
    return serialize(document)


@router.post("/knowledge/reindex", dependencies=[Depends(admin)])
async def reindex(db: DB, rt: RT):
    await rt.rag.reindex(db)
    return {"indexed": True}


@router.delete("/knowledge/{identity}", status_code=204, dependencies=[Depends(admin)])
async def delete_knowledge(identity: int, db: DB):
    await indexing_lock(db)
    document = await require(db, KnowledgeDocument, identity, lock=True)
    if document.source.startswith(("product:", "faq:")):
        raise HTTPException(409, "Delete or edit the original product/FAQ")
    await db.delete(document)


@router.get("/conversations", dependencies=[Depends(staff)])
async def conversations(db: DB, offset: Offset = 0, limit: Limit = 50):
    return [serialize(row) for row in await listing(db, Conversation, offset, limit)]


@router.get("/conversations/{identity}", dependencies=[Depends(staff)])
async def conversation(identity: int, db: DB, after_id: int = 0, limit: Limit = 100):
    row = await require(db, Conversation, identity)
    user = await require(db, User, row.user_id)
    messages = (
        await db.scalars(
            select(Message)
            .where(Message.conversation_id == identity, Message.id > after_id)
            .order_by(Message.id)
            .limit(limit)
        )
    ).all()
    return {
        **serialize(row),
        "user": serialize(user),
        "messages": [serialize(m) for m in messages],
        "next_after_id": messages[-1].id if len(messages) == limit else None,
    }


@router.get("/support/requests", dependencies=[Depends(staff)])
async def requests(db: DB, offset: Offset = 0, limit: Limit = 50):
    rows = await db.execute(
        select(SupportRequest, User)
        .join(Conversation, Conversation.id == SupportRequest.conversation_id)
        .join(User, User.id == Conversation.user_id)
        .order_by(SupportRequest.id.desc())
        .offset(offset)
        .limit(limit)
    )
    return [{**serialize(row), "user": serialize(user)} for row, user in rows]


@router.post("/support/{identity}/take")
async def take_request(identity: int, db: DB, identity_operator: Operator):
    return serialize(await take(db, identity, identity_operator))


@router.post("/support/{identity}/message", status_code=201)
async def reply(identity: int, data: TextIn, db: DB, identity_operator: Operator):
    return serialize(await operator_action(db, identity, identity_operator, data.text))


@router.post("/support/{identity}/close")
async def close(identity: int, db: DB, identity_operator: Operator):
    return serialize(await operator_action(db, identity, identity_operator))


@router.get("/support/delivery", dependencies=[Depends(staff)])
async def delivery(db: DB, offset: Offset = 0, limit: Limit = 50):
    return [serialize(row) for row in await listing(db, Outbox, offset, limit)]


@router.post("/support/delivery/{identity}/retry", dependencies=[Depends(admin)])
async def retry_delivery(identity: int, db: DB):
    from app.database.models import utcnow

    row = await require(db, Outbox, identity, lock=True)
    if row.status != "failed":
        raise HTTPException(409, "Only failed deliveries can be retried")
    row.status, row.attempts, row.next_attempt_at = "pending", 0, utcnow()
    return {"queued": True}


@router.get("/support/{identity}", dependencies=[Depends(staff)])
async def support_detail(identity: int, db: DB):
    return serialize(await require(db, SupportRequest, identity))


@router.post("/chat", dependencies=[Depends(bot_auth)])
async def chat_endpoint(data: ChatIn, db: DB, rt: RT):
    return await chat(db, rt, data)


@router.post("/feedback", dependencies=[Depends(bot_auth)])
async def feedback_endpoint(data: FeedbackIn, db: DB):
    return await feedback(db, data)


@router.post("/debug/search", dependencies=[Depends(admin)])
async def debug(data: TextIn, db: DB, rt: RT):
    return await rt.rag.search(db, data.text)
