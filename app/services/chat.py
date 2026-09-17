import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy import select, text

from app.core.config import settings
from app.database.models import Conversation, Feedback, IncomingEvent, Message, SupportRequest, User, utcnow
from app.services.limits import rate_limit
from app.services.shop import WELCOME, shop_reply
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
    restart = data.text.casefold().strip().split("@")[0] in {"/start", "/bot", "/cancel"}
    if restart and conversation is not None:
        request = await session.scalar(
            select(SupportRequest).where(SupportRequest.conversation_id == conversation.id).with_for_update()
        )
        if request is not None:
            request.status, request.closed_at = "closed", utcnow()
        conversation.status, conversation.updated_at = "closed", utcnow()
        await session.flush()
        conversation = None
    if conversation is None:
        conversation = Conversation(user_id=user.id)
        session.add(conversation)
        await session.flush()
    explicit = asks_operator(data.text)
    if conversation.status == "bot" and not explicit and not restart:
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
    shop = await shop_reply(session, data.text) if conversation.status == "bot" and not restart else None
    session.add(
        Message(
            conversation_id=conversation.id,
            sender_type="user",
            content=shop.subject if shop and shop.subject else data.text,
        )
    )
    conversation.updated_at = utcnow()
    log.info("user_message conversation=%s chars=%s", conversation.id, len(data.text))
    sources, offer, reason = [], False, None
    if restart:
        answer = WELCOME
    elif explicit:
        await handoff(session, conversation, "user_requested")
        answer = HANDOFF
    elif conversation.status != "bot":
        answer = "Сообщение сохранено для оператора. Ожидайте ответа в этом чате или нажмите «Вернуться к AI», чтобы продолжить со мной."
    elif shop is not None:
        answer = shop.answer
    else:
        try:
            async with asyncio.timeout(45):
                # Retrieve the current question first so a topic change cannot be buried by history.
                hits = await runtime.rag.search(session, data.text)
                recent_questions = [
                    m.content[:800]
                    for m in history
                    if m.sender_type == "user" and not m.content.startswith("/")
                ][-2:]
                if recent_questions:
                    try:
                        contextual = await asyncio.wait_for(
                            runtime.rag.search(session, "\n".join([*recent_questions, data.text])), timeout=8
                        )
                    except Exception as exc:
                        from sqlalchemy.exc import SQLAlchemyError

                        if isinstance(exc, SQLAlchemyError):
                            raise
                        log.warning("contextual_search_unavailable type=%s", type(exc).__name__)
                        contextual = []
                    seen = {hit["id"] for hit in hits}
                    hits.extend(hit for hit in contextual if hit["id"] not in seen)
                relevant = [hit for hit in hits if hit["score"] >= settings().similarity_threshold]
                if not relevant:
                    answer, offer, reason = UNKNOWN, True, "low_similarity"
                else:
                    messages = [
                        {
                            "role": "user" if m.sender_type == "user" else "assistant",
                            "content": m.content[:1500],
                        }
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
        "choices": shop.choices if shop else [],
        "show_menu": restart or shop is not None or reason == "provider_unavailable",
        "can_vote": conversation.status == "bot"
        and not restart
        and not offer
        and (bool(sources) or (shop is not None and shop.can_vote)),
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
    return {"saved": True, "offer_operator": not data.helpful and conversation.status == "bot"}
