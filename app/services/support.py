import logging

from fastapi import HTTPException
from sqlalchemy import select

from app.database.models import Conversation, Message, Outbox, SupportRequest, User, utcnow
from app.database.repositories import require

log = logging.getLogger(__name__)
HANDOFF = "Я передал ваш вопрос оператору. Он подключится к диалогу, когда будет доступен."


async def handoff(session, conversation, reason):
    existing = await session.scalar(
        select(SupportRequest).where(SupportRequest.conversation_id == conversation.id)
    )
    if existing is None:
        existing = SupportRequest(conversation_id=conversation.id, reason=reason)
        session.add(existing)
        conversation.status = "waiting_operator"
        conversation.updated_at = utcnow()
        log.info("support_request_created conversation=%s", conversation.id)
    await session.flush()
    return existing


async def locked_request(session, request_id):
    # Same lock order as chat: conversation first, support request second.
    request = await require(session, SupportRequest, request_id)
    conversation = await require(session, Conversation, request.conversation_id, lock=True)
    await session.refresh(request, with_for_update=True)
    return request, conversation


async def take(session, request_id, operator):
    request, conversation = await locked_request(session, request_id)
    if request.status == "closed" or request.assigned_operator_id not in (None, operator):
        raise HTTPException(409, "Обращение уже занято или закрыто")
    request.status, request.assigned_operator_id = "assigned", operator
    conversation.status, conversation.updated_at = "operator", utcnow()
    log.info("operator_connected request=%s", request.id)
    return request


async def operator_action(session, request_id, operator, content=None):
    request, conversation = await locked_request(session, request_id)
    if request.status != "assigned" or request.assigned_operator_id != operator:
        raise HTTPException(409, "Сначала возьмите обращение своим ключом оператора")
    user = await require(session, User, conversation.user_id)
    if content is None:
        request.status, request.closed_at = "closed", utcnow()
        conversation.status = "closed"
        content = "Оператор закрыл обращение. Новое сообщение начнёт новый диалог с AI."
        sender = "system"
    else:
        sender = "operator"
    conversation.updated_at = utcnow()
    message = Message(conversation_id=conversation.id, sender_type=sender, content=content)
    session.add(message)
    session.add(Outbox(telegram_id=user.telegram_id, content=content))
    await session.flush()
    return message
