"""Точка входа: запускает Telegram-бота и планировщик проверок."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .bot import BotApp
from .config import load_settings
from .scheduler import build_scheduler
from .storage import Storage

log = logging.getLogger(__name__)


async def _amain() -> None:
    settings = load_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not settings.tg_bot_token:
        log.error("TG_BOT_TOKEN не задан. Скопируйте .env.example в .env и заполните токен.")
        raise SystemExit(1)

    storage = Storage(settings.db_path)
    bot = Bot(
        token=settings.tg_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    app = BotApp(settings, storage, bot)
    app.register(dp)

    scheduler = build_scheduler(app, settings.poll_interval_minutes)
    scheduler.start()
    log.info("Бот запущен, опрос каждые %d мин.", settings.poll_interval_minutes)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("Остановка")


if __name__ == "__main__":
    main()
