import logging

from fastapi import HTTPException
from sqlalchemy import func, select, text

from app.core.config import settings
from app.database.models import Conversation, Feedback, IncomingEvent, Message, User, utcnow
from app.services.limits import rate_limit
from app.services.support import HANDOFF, handoff

log = logging.getLogger(__name__)
UNKNOWN = "В моей базе знаний недостаточно информации. Могу передать ваш вопрос оператору."
UNAVAILABLE = "Сейчас AI-помощник временно недоступен. Я могу передать ваш вопрос оператору."


def asks_operator(value):
    value = value.casefold().strip()
    return value in {"/operator", "оператор", "позови человека", "передать оператору"} or any(
        phrase in value for phrase in ("хочу поговорить с оператором", "позови оператора", "живого оператора")
    )


async def chat(session, runtime, data):
    # Transaction-scoped lock serializes this user's messages on every backend replica.
    if session.bind.dialect.name == "postgresql":
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": data.telegram_id})
    event_key = f"{data.telegram_id}:{data.event_id}"
    previous = await session.scalar(select(IncomingEvent).where(IncomingEvent.event_id == event_key))
    if previous:
        return previous.response
    user = await session.scalar(select(User).where(User.telegram_id == data.telegram_id))
    if user is None:
        user = User(telegram_id=data.telegram_id, username=data.username, first_name=data.first_name)
        session.add(user)
        await session.flush()
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.user_id == user.id, Conversation.status != "closed")
        .with_for_update()
    )
    if conversation is None:
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
    explicit = asks_operator(data.text)
    if conversation.status == "bot" and not explicit:
        await rate_limit(runtime.redis, data.telegram_id)
    history = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.id.desc())
                .limit(settings().history_limit)
            )
        ).all()
    )[::-1]
    session.add(Message(conversation_id=conversation.id, sender_type="user", content=data.text))
    conversation.updated_at = utcnow()
    log.info("user_message conversation=%s chars=%s", conversation.id, len(data.text))
    sources, offer, reason = [], False, None
    if explicit:
        await handoff(session, conversation, "user_requested")
        answer = HANDOFF
    elif conversation.status != "bot":
        answer = "Сообщение сохранено для оператора. Ожидайте ответа в этом чате."
    else:
        try:
            # Include recent user subjects so follow-ups such as 'сколько он стоит' retrieve the product.
            recent_questions = [m.content[:800] for m in history if m.sender_type == "user"][-2:]
            query = "\n".join([*recent_questions, data.text])
            hits = await runtime.rag.search(session, query)
            relevant = [hit for hit in hits if hit["score"] >= settings().similarity_threshold]
            if not relevant:
                answer, offer, reason = UNKNOWN, True, "low_similarity"
            else:
                messages = [
                    {"role": "user" if m.sender_type == "user" else "assistant", "content": m.content[:1500]}
                    for m in history
                    if m.sender_type in ("user", "assistant")
                ]
                messages.append({"role": "user", "content": data.text})
                result = await runtime.llm.generate_answer(messages, relevant)
                allowed = {hit["id"] for hit in relevant}
                if result.insufficient or not result.source_ids or not set(result.source_ids) <= allowed:
                    answer, offer, reason = UNKNOWN, True, "insufficient_evidence"
                else:
                    answer = result.answer
                    sources = [
                        {"id": h["id"], "title": h["title"], "source": h["source"]}
                        for h in relevant
                        if h["id"] in result.source_ids
                    ]
        except Exception as exc:
            # A failed SQL transaction cannot be used for saving a fallback; global DB handler rolls it back.
            from sqlalchemy.exc import SQLAlchemyError

            if isinstance(exc, SQLAlchemyError):
                raise
            log.error("rag_or_llm_failed type=%s", type(exc).__name__)
            answer, offer, reason = UNAVAILABLE, True, "provider_unavailable"
    message = Message(
        conversation_id=conversation.id, sender_type="assistant", content=answer, sources=sources
    )
    session.add(message)
    await session.flush()
    response = {
        "conversation_id": conversation.id,
        "message_id": message.id,
        "answer": answer,
        "sources": sources,
        "offer_operator": offer,
        "reason": reason,
        "status": conversation.status,
    }
    session.add(IncomingEvent(event_id=event_key, response=response))
    return response


async def feedback(session, data):
    user = await session.scalar(select(User).where(User.telegram_id == data.telegram_id))
    message = await session.get(Message, data.message_id)
    if user is None or message is None or message.sender_type != "assistant":
        raise HTTPException(404, "Message not found")
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == message.conversation_id).with_for_update()
    )
    if conversation.user_id != user.id:
        raise HTTPException(404, "Message not found")
    row = await session.scalar(select(Feedback).where(Feedback.message_id == message.id))
    if row:
        row.helpful = data.helpful
    else:
        session.add(Feedback(user_id=user.id, message_id=message.id, helpful=data.helpful))
    await session.flush()
    negatives = await session.scalar(
        select(func.count())
        .select_from(Feedback)
        .join(Message)
        .where(Message.conversation_id == conversation.id, Feedback.helpful.is_(False))
    )
    return {"saved": True, "offer_operator": negatives >= 2 and conversation.status == "bot"}
