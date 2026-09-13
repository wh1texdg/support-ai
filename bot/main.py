import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.logging import configure_logging

router = Router()
log = logging.getLogger(__name__)


class ResilientStorage(RedisStorage):
    """FSM is only a UI hint. Backend owns the authoritative conversation state."""

    async def get_state(self, key):
        try:
            return await super().get_state(key)
        except RedisError:
            log.warning("fsm_read_unavailable")
            return None

    async def set_state(self, key, state=None):
        try:
            await super().set_state(key, state)
        except RedisError:
            log.warning("fsm_write_unavailable")


def keyboard(message_id=None, offer=False):
    rows = []
    if message_id:
        rows.append(
            [
                InlineKeyboardButton(text="👍 Полезно", callback_data=f"vote:{message_id}:1"),
                InlineKeyboardButton(text="👎 Не помогло", callback_data=f"vote:{message_id}:0"),
            ]
        )
    rows.append([InlineKeyboardButton(text="Передать оператору", callback_data="operator")])
    if offer:
        rows.append([InlineKeyboardButton(text="Продолжить диалог", callback_data="continue")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_chat(client, user, text, event_id, target):
    payload = {
        "telegram_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "text": text,
        "event_id": event_id,
    }
    try:
        response = await client.post("/api/chat", json=payload)
        if response.status_code in (429, 503):
            await target.answer(response.json()["detail"], reply_markup=keyboard())
            return
        response.raise_for_status()
        data = response.json()
        answer = data["answer"]
        if data["sources"]:
            answer += "\n\nИсточники: " + "; ".join(s["title"][:100] for s in data["sources"])
        await target.answer(
            answer,
            reply_markup=keyboard(
                data["message_id"] if data["status"] == "bot" else None, data["offer_operator"]
            ),
        )
    except (httpx.HTTPError, ValueError, KeyError):
        log.error("backend_request_failed")
        await target.answer("Сервис временно недоступен. Попробуйте позже или /operator.")


@router.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Здравствуйте! Я SupportAI. Отвечаю по базе знаний магазина. "
        "Для связи с человеком используйте /operator. Не присылайте пароли и данные карты.",
        reply_markup=keyboard(),
    )


@router.message(F.text)
async def text_message(message: Message, client):
    if message.chat.type != "private":
        await message.answer("Пожалуйста, напишите боту в личном чате.")
        return
    if len(message.text) > 3000:
        await message.answer("Сократите сообщение до 3000 символов.")
        return
    await send_chat(client, message.from_user, message.text, f"msg:{message.message_id}", message)


@router.callback_query(F.data == "operator")
async def operator_callback(callback: CallbackQuery, client, state):
    await callback.answer()
    await send_chat(client, callback.from_user, "/operator", f"callback:{callback.id}", callback.message)
    await state.set_state("operator_requested")


@router.callback_query(F.data == "continue")
async def continue_callback(callback: CallbackQuery, state):
    await state.set_state(None)
    await callback.answer("Напишите следующий вопрос в чат")


@router.callback_query(F.data.startswith("vote:"))
async def vote(callback: CallbackQuery, client):
    _, message_id, value = callback.data.split(":")
    try:
        response = await client.post(
            "/api/feedback",
            json={
                "telegram_id": callback.from_user.id,
                "message_id": int(message_id),
                "helpful": value == "1",
            },
        )
        response.raise_for_status()
        await callback.answer("Спасибо за отзыв")
        if response.json()["offer_operator"]:
            await callback.message.answer(
                "Похоже, мои ответы не помогли. Подключить оператора?", reply_markup=keyboard(offer=True)
            )
    except (httpx.HTTPError, ValueError):
        await callback.answer("Не удалось сохранить отзыв. Попробуйте позже.")


@router.errors()
async def errors(event):
    log.error("telegram_handler_failed type=%s", type(event.exception).__name__)
    return True


async def main():
    configure_logging(settings().log_level)
    token = settings().bot_token.get_secret_value()
    if not token:
        log.warning("bot_disabled_no_token")
        await asyncio.Event().wait()
        return
    storage = ResilientStorage.from_url(
        settings().redis_url,
        state_ttl=3600,
        connection_kwargs={"socket_connect_timeout": 2, "socket_timeout": 2},
    )
    dispatcher = Dispatcher(storage=storage)
    dispatcher.include_router(router)
    async with (
        Bot(token) as bot,
        httpx.AsyncClient(
            base_url=settings().backend_url,
            timeout=100,
            headers={"Authorization": "Bearer " + settings().bot_api_key.get_secret_value()},
        ) as client,
    ):
        try:
            await dispatcher.start_polling(bot, client=client)
        finally:
            await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
