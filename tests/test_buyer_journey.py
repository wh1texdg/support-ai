from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.database.models import Message, SupportRequest
from app.scripts.seed import seed
from app.services.llm import Answer
from bot.main import configure_profile, keyboard, send_chat, stay_callback, vote
from tests.test_api import BOT, send


async def test_buyer_journey_without_ai(client, session, runtime):
    await seed(session)
    await session.commit()
    start = (await send(client, "/start", "start")).json()
    assert start["show_menu"] and not start["offer_operator"]
    assert "каталог" in start["answer"].lower() or "товар" in start["answer"].lower()
    catalog = (await send(client, "/catalog", "catalog")).json()
    category = next(c for c in catalog["choices"] if c["text"] == "Наушники")
    products = (await send(client, category["action"], "category")).json()
    card = (await send(client, products["choices"][0]["action"], "product")).json()
    assert "Sound Demo Pro" in card["answer"] and "Цена:" in card["answer"]
    assert card["can_vote"]
    review = await client.post(
        "/api/feedback",
        headers=BOT,
        json={"telegram_id": 42, "message_id": card["message_id"], "helpful": False},
    )
    assert review.json()["offer_operator"]
    assert await session.scalar(select(SupportRequest)) is None
    stayed = (await send(client, "/continue", "stay")).json()
    assert stayed["conversation_id"] == card["conversation_id"]
    history = (
        await session.scalars(select(Message).where(Message.conversation_id == card["conversation_id"]))
    ).all()
    assert any(m.sender_type == "user" and "Sound Demo Pro" in m.content for m in history)
    for index, command in enumerate(["/delivery", "/payment", "/returns"]):
        response = (await send(client, command, f"section{index}")).json()
        assert response["can_vote"] and not response["offer_operator"]
    runtime.llm.generate_answer.assert_not_awaited()
    runtime.rag.search.assert_not_awaited()
    handoff = (await send(client, "/operator", "operator")).json()
    assert handoff["status"] == "waiting_operator"
    assert (await send(client, "/continue", "stale")).json()["status"] == "waiting_operator"
    assert (await send(client, "/bot", "back")).json()["status"] == "bot"


async def test_current_answer_survives_context_lookup_outage(client, runtime):
    await send(client, "Первый вопрос", "first")
    hit = {"id": 1, "score": 0.9, "title": "Доставка", "source": "faq:1", "content": "Доставка по России"}
    runtime.rag.search.side_effect = [[hit], TimeoutError()]
    runtime.llm.generate_answer.return_value = Answer(
        answer="Доставка по России", insufficient=False, source_ids=[1]
    )
    result = (await send(client, "Куда доставляете?", "second")).json()
    assert not result["offer_operator"] and result["sources"]


async def test_telegram_answer_hides_sources(client, runtime):
    runtime.rag.search.return_value = [
        {"id": 1, "score": 0.9, "title": "PRIVATE SOURCE", "source": "faq:1", "content": "Доставка"}
    ]
    target = SimpleNamespace(answer=AsyncMock())
    user = SimpleNamespace(id=42, username=None, first_name=None)
    # The bot client normally has its auth header configured at construction.
    client.headers.update(BOT)
    await send_chat(client, user, "Куда доставляете?", "telegram-question", target)
    text = target.answer.await_args.args[0]
    assert "PRIVATE SOURCE" not in text and "Источники" not in text
    assert "vote:" in str(target.answer.await_args.kwargs["reply_markup"])


async def test_negative_feedback_offers_choice_and_stay_keeps_history(client):
    card = (await send(client, "Тестовый вопрос", "q")).json()
    client.headers.update(BOT)
    message = SimpleNamespace(answer=AsyncMock(), edit_reply_markup=AsyncMock())
    callback = SimpleNamespace(
        data=f"vote:{card['message_id']}:0",
        from_user=SimpleNamespace(id=42, username=None, first_name=None),
        answer=AsyncMock(),
        message=message,
        id="stay-callback",
    )
    await vote(callback, client)
    buttons = message.answer.await_args.kwargs["reply_markup"]
    assert {b.callback_data for row in buttons.inline_keyboard for b in row} == {"operator", "stay"}
    await stay_callback(callback, client)
    assert "переписка сохранена" in message.answer.await_args.args[0]
    same = (await send(client, "Следующий вопрос", "next")).json()
    assert same["conversation_id"] == card["conversation_id"]


async def test_bot_profile_includes_description_and_commands():
    bot = SimpleNamespace(
        **{
            name: AsyncMock()
            for name in [
                "set_my_description",
                "set_my_short_description",
                "set_my_commands",
                "set_chat_menu_button",
            ]
        }
    )
    await configure_profile(bot)
    assert "Нажмите" in bot.set_my_description.await_args.kwargs["description"]
    commands = bot.set_my_commands.await_args.args[0]
    assert {"catalog", "operator", "bot", "help"} <= {c.command for c in commands}
    for row in keyboard(show_menu=True).inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


async def test_follow_up_uses_context_when_primary_lookup_times_out(client, runtime):
    await send(client, "Sound Demo Pro", "subject")
    hit = {
        "id": 1,
        "score": 0.9,
        "title": "Sound Demo Pro",
        "source": "product:11",
        "content": "Гарантия 12 месяцев",
    }
    runtime.rag.search.side_effect = [TimeoutError(), [hit]]
    runtime.llm.generate_answer.return_value = Answer(
        answer="Гарантия 12 месяцев", insufficient=False, source_ids=[1]
    )
    result = (await send(client, "Какая у них гарантия?", "follow-up")).json()
    assert not result["offer_operator"] and result["sources"]
