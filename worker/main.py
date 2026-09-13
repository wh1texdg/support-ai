import asyncio
import logging
from datetime import timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import exists, select
from sqlalchemy.orm import aliased

from app.core.config import settings
from app.core.logging import configure_logging
from app.database.models import Outbox, utcnow
from app.database.session import Session

log = logging.getLogger(__name__)


async def deliver_one(bot):
    async with Session() as session, session.begin():
        older = aliased(Outbox)
        row = await session.scalar(
            select(Outbox)
            .where(
                Outbox.status == "pending",
                Outbox.next_attempt_at <= utcnow(),
                ~exists(
                    select(older.id).where(
                        older.telegram_id == Outbox.telegram_id,
                        older.id < Outbox.id,
                        older.status == "pending",
                    )
                ),
            )
            .order_by(Outbox.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        row.attempts += 1
        try:
            await bot.send_message(row.telegram_id, row.content, request_timeout=15)
            row.status, row.sent_at = "sent", utcnow()
        except TelegramRetryAfter as exc:
            row.next_attempt_at = utcnow() + timedelta(seconds=exc.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest):
            row.status = "failed"
            log.error("telegram_delivery_permanent_failure outbox=%s", row.id)
        except Exception as exc:
            row.next_attempt_at = utcnow() + timedelta(seconds=min(300, 2 ** min(row.attempts, 9)))
            log.warning("telegram_delivery_retry outbox=%s type=%s", row.id, type(exc).__name__)
        if row.status == "pending" and row.attempts >= 8:
            row.status = "failed"
        return True


async def main():
    configure_logging(settings().log_level)
    token = settings().bot_token.get_secret_value()
    if not token:
        log.warning("delivery_disabled_no_token")
        await asyncio.Event().wait()
        return
    async with Bot(token) as bot:
        while True:
            try:
                found = await deliver_one(bot)
            except Exception as exc:
                log.error("outbox_worker_failed type=%s", type(exc).__name__)
                found = False
            if not found:
                await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
