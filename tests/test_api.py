from sqlalchemy import func, select

from app.database.models import Feedback, KnowledgeChunk, KnowledgeDocument, Message, Outbox, SupportRequest

ADMIN = {"Authorization": "Bearer test-admin"}
BOT = {"Authorization": "Bearer test-bot"}
OPERATOR = {"Authorization": "Bearer test-operator"}
OTHER = {"Authorization": "Bearer other-operator"}
PRODUCT = {"name": "Demo", "description": "Demo phone", "category": "phones", "price": "199.90", "stock": 2}


async def send(client, text="Неизвестный вопрос", event="1", user=42):
    return await client.post(
        "/api/chat", headers=BOT, json={"text": text, "telegram_id": user, "event_id": event}
    )


async def test_auth_and_validation(client):
    assert (await client.get("/api/products")).status_code == 401
    assert (await client.get("/api/products", headers=BOT)).status_code == 401
    assert (
        await client.post("/api/products", headers=ADMIN, json={**PRODUCT, "stock": -1})
    ).status_code == 422
    assert (await client.get("/api/products?limit=1000", headers=ADMIN)).status_code == 422


async def test_crud_sync_and_cascade(client, session):
    response = await client.post("/api/products", headers=ADMIN, json=PRODUCT)
    assert response.status_code == 201, response.text
    identity = response.json()["id"]
    doc = await session.scalar(select(KnowledgeDocument))
    assert "199.90" in doc.content
    assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 1
    response = await client.patch(
        f"/api/products/{identity}", headers=ADMIN, json={"price": "99.90", "stock": 0}
    )
    assert response.status_code == 200
    await session.refresh(doc)
    assert "99.90" in doc.content and "Остаток: 0" in doc.content
    assert (
        await client.patch(f"/api/products/{identity}", headers=ADMIN, json={"name": None})
    ).status_code == 422
    assert (await client.delete(f"/api/products/{identity}", headers=ADMIN)).status_code == 204
    assert await session.scalar(select(func.count()).select_from(KnowledgeChunk)) == 0
    assert (await client.get(f"/api/products/{identity}", headers=ADMIN)).status_code == 404


async def test_unknown_and_idempotency(client, session, runtime):
    first = await send(client)
    assert first.status_code == 200, first.text
    assert first.json()["offer_operator"] is True
    assert first.json()["reason"] == "low_similarity"
    second = await send(client)
    assert second.json() == first.json()
    runtime.llm.generate_answer.assert_not_awaited()
    assert await session.scalar(select(func.count()).select_from(Message)) == 2


async def test_handoff_operator_delivery_and_new_conversation(client, session, runtime):
    first = (await send(client, "Позови человека")).json()
    assert first["status"] == "waiting_operator"
    await send(client, "/operator", "2")
    assert await session.scalar(select(func.count()).select_from(SupportRequest)) == 1
    request = (await client.get("/api/support/requests", headers=OPERATOR)).json()[0]
    identity = request["id"]
    assert (await client.get(f"/api/support/{identity}", headers=OPERATOR)).json()["status"] == "waiting"
    assert request["user"]["telegram_id"] == 42
    assert (
        await client.post(f"/api/support/{identity}/message", headers=OPERATOR, json={"text": "hi"})
    ).status_code == 409
    assert (await client.post(f"/api/support/{identity}/take", headers=OPERATOR)).status_code == 200
    assert (await client.post(f"/api/support/{identity}/take", headers=OTHER)).status_code == 409
    result = await send(client, "Мой вопрос", "3")
    assert result.json()["status"] == "operator"
    runtime.rag.search.assert_not_awaited()
    assert (
        await client.post(f"/api/support/{identity}/message", headers=OTHER, json={"text": "hi"})
    ).status_code == 409
    assert (
        await client.post(f"/api/support/{identity}/message", headers=OPERATOR, json={"text": "Здравствуйте"})
    ).status_code == 201
    assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
    assert (await client.post(f"/api/support/{identity}/close", headers=OPERATOR)).status_code == 200
    assert (
        await client.post(f"/api/support/{identity}/message", headers=OPERATOR, json={"text": "late"})
    ).status_code == 409
    next_chat = (await send(client, "Доставка?", "4")).json()
    assert next_chat["conversation_id"] != first["conversation_id"]


async def test_feedback_ownership_and_repeated_negative(client, session):
    one = (await send(client)).json()
    two = (await send(client, event="2")).json()
    vote = {"telegram_id": 99, "message_id": one["message_id"], "helpful": False}
    assert (await client.post("/api/feedback", headers=BOT, json=vote)).status_code == 404
    vote["telegram_id"] = 42
    assert (await client.post("/api/feedback", headers=BOT, json=vote)).json()["offer_operator"]
    await client.post("/api/feedback", headers=BOT, json=vote)
    vote["message_id"] = two["message_id"]
    assert (await client.post("/api/feedback", headers=BOT, json=vote)).json()["offer_operator"]
    assert await session.scalar(select(func.count()).select_from(Feedback)) == 2


async def test_rate_limit_and_handoff_escape(client):
    for number in range(10):
        assert (await send(client, event=str(number))).status_code == 200
    assert (await send(client, event="11")).status_code == 429
    assert (await send(client, text="/operator", event="12")).json()["status"] == "waiting_operator"


async def test_faq_and_knowledge(client):
    data = {"question": "Question?", "answer": "Answer.", "category": "test"}
    result = await client.post("/api/faq", headers=ADMIN, json=data)
    assert result.status_code == 201
    identity = result.json()["id"]
    assert (
        await client.patch(f"/api/faq/{identity}", headers=ADMIN, json={"answer": "New"})
    ).status_code == 200
    assert (await client.get("/api/faq", headers=ADMIN)).json()[0]["answer"] == "New"
    result = await client.post(
        "/api/knowledge", headers=ADMIN, json={"title": "Title", "content": "Body", "source": "manual:test"}
    )
    assert result.status_code == 201
    assert (await client.post("/api/knowledge/reindex", headers=ADMIN)).status_code == 200
    assert (await client.delete(f"/api/knowledge/{result.json()['id']}", headers=ADMIN)).status_code == 204
    assert (await client.delete(f"/api/faq/{identity}", headers=ADMIN)).status_code == 204


async def test_embedding_failure_rolls_back_catalog(client, session, runtime):
    import httpx
    from openai import APIConnectionError

    from app.database.models import Product

    runtime.rag.embeddings.create_embeddings.side_effect = APIConnectionError(
        request=httpx.Request("POST", "https://example.test")
    )
    response = await client.post("/api/products", headers=ADMIN, json=PRODUCT)
    assert response.status_code == 503
    assert await session.scalar(select(func.count()).select_from(Product)) == 0
    assert await session.scalar(select(func.count()).select_from(KnowledgeDocument)) == 0


async def test_return_to_ai_closes_request_and_resumes_search(client, session, runtime):
    first = (await send(client, "/operator")).json()
    request = await session.scalar(select(SupportRequest))
    reset = (await send(client, "/bot", "reset")).json()
    await session.refresh(request)
    assert request.status == "closed"
    assert reset["status"] == "bot"
    assert reset["conversation_id"] != first["conversation_id"]
    assert reset["offer_operator"] is False
    assert (await send(client, "/bot", "reset")).json() == reset
    assert (await client.post(f"/api/support/{request.id}/take", headers=OPERATOR)).status_code == 409
    await send(client, "Доставка?", "next")
    runtime.rag.search.assert_awaited_once()


async def test_start_leaves_assigned_operator(client):
    await send(client, "/operator")
    request = (await client.get("/api/support/requests", headers=OPERATOR)).json()[0]
    await client.post(f"/api/support/{request['id']}/take", headers=OPERATOR)
    assert (await send(client, "/start", "start")).json()["status"] == "bot"
    response = await client.post(
        f"/api/support/{request['id']}/message", headers=OPERATOR, json={"text": "late"}
    )
    assert response.status_code == 409


def test_bot_buttons_match_conversation_state():
    from bot.main import keyboard

    assert keyboard() is None
    assert all(b.callback_data != "operator" for row in keyboard(12).inline_keyboard for b in row)
    assert keyboard(offer=True).inline_keyboard[0][0].callback_data == "operator"
    assert keyboard(operator_mode=True).inline_keyboard[0][0].callback_data == "continue"


async def test_topic_change_keeps_current_question_sources(client, runtime):
    from app.services.llm import Answer

    await send(client, "Доставка?", "delivery")
    runtime.rag.search.reset_mock()
    store = {"id": 9, "score": 0.8, "title": "Магазин", "source": "store", "content": "Демо-магазин"}
    old = {"id": 1, "score": 0.9, "title": "Доставка", "source": "delivery", "content": "Доставка"}
    runtime.rag.search.side_effect = [[store], [old]]
    runtime.llm.generate_answer.return_value = Answer(
        answer="Демо-магазин", insufficient=False, source_ids=[9]
    )
    result = (await send(client, "Расскажи подробнее о вашем магазине", "store")).json()
    assert result["sources"][0]["id"] == 9
    assert runtime.rag.search.await_args_list[0].args[1] == "Расскажи подробнее о вашем магазине"
    context = runtime.llm.generate_answer.await_args.args[1]
    assert [hit["id"] for hit in context] == [9, 1]
