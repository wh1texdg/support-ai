import asyncio
import logging

import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
)
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.logging import configure_logging
from app.services.shop import HELP

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


def menu_rows():
    return [
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="shop:/catalog")],
        [
            InlineKeyboardButton(text="🚚 Доставка", callback_data="shop:/delivery"),
            InlineKeyboardButton(text="💳 Оплата", callback_data="shop:/payment"),
        ],
        [
            InlineKeyboardButton(text="📦 Возврат", callback_data="shop:/returns"),
            InlineKeyboardButton(text="❓ Помощь", callback_data="help"),
        ],
    ]


def keyboard(message_id=None, offer=False, operator_mode=False, choices=None, show_menu=False):
    rows = []
    if message_id:
        rows.append(
            [
                InlineKeyboardButton(text="👍 Полезно", callback_data=f"vote:{message_id}:1"),
                InlineKeyboardButton(text="👎 Не помогло", callback_data=f"vote:{message_id}:0"),
            ]
        )
    if offer and not operator_mode:
        rows.append([InlineKeyboardButton(text="Передать оператору", callback_data="operator")])
        rows.append([InlineKeyboardButton(text="Остаться с ботом", callback_data="stay")])
    if operator_mode:
        rows.append([InlineKeyboardButton(text="Вернуться к AI", callback_data="continue")])
    for choice in choices or []:
        rows.append([InlineKeyboardButton(text=choice["text"], callback_data="shop:" + choice["action"])])
    if show_menu and not operator_mode:
        rows.extend(menu_rows())
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def clear_buttons(message):
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        # An old or already-edited message must not block the selected action.
        log.info("old_keyboard_unavailable")


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
            await target.answer(response.json()["detail"], reply_markup=keyboard(show_menu=True))
            return
        response.raise_for_status()
        data = response.json()
        answer = data["answer"]
        await target.answer(
            answer,
            reply_markup=keyboard(
                data["message_id"]
                if data.get("can_vote", bool(data["sources"])) and data["status"] == "bot"
                else None,
                data["offer_operator"],
                data["status"] != "bot",
                choices=data.get("choices"),
                show_menu=data.get("show_menu", False),
            ),
        )
    except (httpx.HTTPError, ValueError, KeyError):
        log.error("backend_request_failed")
        await target.answer(
            "Не удалось получить ответ. Попробуйте ещё раз или выберите раздел меню.",
            reply_markup=keyboard(show_menu=True, offer=True),
        )


@router.message(Command("start", "bot", "cancel"))
async def start(message: Message, client):
    if message.chat.type != "private":
        return
    await send_chat(client, message.from_user, message.text, f"msg:{message.message_id}", message)


@router.message(Command("help", "menu"))
async def help_message(message: Message):
    if message.chat.type == "private":
        await message.answer(HELP, reply_markup=keyboard(show_menu=True))


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(HELP, reply_markup=keyboard(show_menu=True, offer=True))


@router.callback_query(F.data.startswith("shop:"))
async def shop_callback(callback: CallbackQuery, client):
    await callback.answer()
    await send_chat(
        client,
        callback.from_user,
        callback.data.removeprefix("shop:"),
        f"callback:{callback.id}",
        callback.message,
    )


@router.callback_query(F.data == "stay")
async def stay_callback(callback: CallbackQuery, client):
    await callback.answer()
    await send_chat(client, callback.from_user, "/continue", f"callback:{callback.id}", callback.message)
    await clear_buttons(callback.message)


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
    await clear_buttons(callback.message)


@router.callback_query(F.data == "continue")
async def continue_callback(callback: CallbackQuery, client, state):
    await callback.answer()
    await send_chat(client, callback.from_user, "/bot", f"callback:{callback.id}", callback.message)
    await state.set_state(None)
    await clear_buttons(callback.message)


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
        await clear_buttons(callback.message)
        if response.json()["offer_operator"]:
            await callback.message.answer(
                "Ответ не помог. Передать диалог оператору или продолжить со мной?",
                reply_markup=keyboard(offer=True),
            )
    except (httpx.HTTPError, ValueError):
        await callback.answer("Не удалось сохранить отзыв. Попробуйте позже.")


@router.message()
async def unsupported_message(message: Message):
    if message.chat.type == "private":
        await message.answer(
            "Пока я понимаю текстовые сообщения. Опишите вопрос текстом или откройте меню.",
            reply_markup=keyboard(show_menu=True),
        )


async def configure_profile(bot):
    await bot.set_my_description(
        description="Помощник демо-магазина электроники SupportAI. Каталог и наличие товаров, доставка, оплата, возврат и связь с оператором. Нажмите «Начать». Проект для портфолио, реальные покупки не оформляются."
    )
    await bot.set_my_short_description(
        short_description="Демо-магазин электроники: товары, доставка, оплата и помощь оператора."
    )
    await bot.set_my_commands(
        [
            BotCommand(command=c, description=d)
            for c, d in [
                ("start", "Начать заново"),
                ("menu", "Разделы магазина"),
                ("catalog", "Каталог товаров"),
                ("delivery", "Доставка"),
                ("payment", "Оплата"),
                ("returns", "Возврат товара"),
                ("help", "Что умеет бот"),
                ("operator", "Связаться с оператором"),
                ("bot", "Вернуться к боту"),
            ]
        ]
    )
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


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
            try:
                await asyncio.wait_for(configure_profile(bot), timeout=20)
            except (TelegramAPIError, TimeoutError):
                log.warning("bot_profile_setup_unavailable")
            await dispatcher.start_polling(bot, client=client)
        finally:
            await storage.close()


if __name__ == "__main__":
    asyncio.run(main())
