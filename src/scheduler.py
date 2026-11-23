"""Планировщик периодических проверок (APScheduler)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .bot import BotApp
from .scraper import CaptchaDetected

log = logging.getLogger(__name__)


def build_scheduler(app: BotApp, interval_minutes: int) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="Europe/Moscow")

    async def job() -> None:
        log.info("Плановая проверка ГАС «Правосудие»…")
        try:
            n = await app.run_check()
            log.info("Проверка завершена, новых/изменённых: %d", n)
        except CaptchaDetected:
            log.warning("Капча — уведомляю админа и жду следующего цикла")
            chat_id = app.settings.effective_chat_id()
            if chat_id:
                await app._send(chat_id, "⚠️ ГАС «Правосудие» требует капчу. Проверьте VPS вручную.")
        except Exception:  # noqa: BLE001
            log.exception("Ошибка плановой проверки")

    scheduler.add_job(
        job,
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="poll",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now() + timedelta(seconds=5),
    )
    return scheduler
