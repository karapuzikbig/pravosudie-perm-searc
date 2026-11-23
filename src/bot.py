"""Telegram-бот (aiogram 3): команды + уведомления о заседаниях."""
from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from .config import Settings
from .parsers import Case, format_case
from .scraper import CaptchaDetected, Scraper
from .storage import Storage

log = logging.getLogger(__name__)
router = Router()


class BotApp:
    def __init__(self, settings: Settings, storage: Storage, bot: Bot) -> None:
        self.settings = settings
        self.storage = storage
        self.bot = bot
        self.muted = bool(storage.get_setting("muted", False))
        self._scraper = Scraper(settings)

    # ------------------------------------------------------------- команды
    def register(self, dp: Dispatcher) -> None:
        dp.include_router(router)

        @router.message(CommandStart())
        async def cmd_start(message: Message) -> None:
            await message.answer(
                "👋 Бот мониторинга заседаний мировых судей г. Перми "
                "(ГАС «Правосудие»).\n\n"
                f"Ваш chat_id: <code>{message.chat.id}</code>\n"
                "Впишите его в ADMIN_CHAT_ID в .env.\n\n"
                "Команды:\n"
                "/articles — отслеживаемые статьи\n"
                "/check — проверить сейчас\n"
                "/status — статистика\n"
                "/upcoming — ближайшие заседания\n"
                "/mute, /unmute — выкл/вкл уведомления"
            )

        @router.message(Command("articles"))
        async def cmd_articles(message: Message) -> None:
            arts = self.storage.get_setting("articles", self.settings.articles)
            await message.answer("Отслеживаемые статьи КоАП:\n" + "\n".join(f"• {a}" for a in arts))

        @router.message(Command("status"))
        async def cmd_status(message: Message) -> None:
            await message.answer(
                f"Дел в базе: <b>{self.storage.count()}</b>\n"
                f"Уведомления: {'🔇 выключены' if self.muted else '🔔 включены'}\n"
                f"Интервал опроса: каждые {self.settings.poll_interval_minutes} мин."
            )

        @router.message(Command("mute"))
        async def cmd_mute(message: Message) -> None:
            self.muted = True
            self.storage.set_setting("muted", True)
            await message.answer("🔇 Уведомления выключены")

        @router.message(Command("unmute"))
        async def cmd_unmute(message: Message) -> None:
            self.muted = False
            self.storage.set_setting("muted", False)
            await message.answer("🔔 Уведомления включены")

        @router.message(Command("upcoming"))
        async def cmd_upcoming(message: Message) -> None:
            rows = self.storage.upcoming_cases(limit=10)
            if not rows:
                await message.answer("Предстоящих заседаний нет.")
                return
            text = "\n\n".join(self._row_to_text(r) for r in rows)
            await self._send(message.chat.id, text)

        @router.message(Command("check"))
        async def cmd_check(message: Message) -> None:
            await message.answer("🔄 Запускаю проверку…")
            try:
                new = await self.run_check()
            except CaptchaDetected:
                await message.answer("⚠️ На сайте появилась капча — проверьте вручную на VPS.")
                return
            except Exception as exc:  # noqa: BLE001
                log.exception("Ошибка ручной проверки")
                await message.answer(f"❌ Ошибка проверки: {exc}")
                return
            await message.answer(f"✅ Готово. Новых заседаний: {new}")

    # --------------------------------------------------------------- логика
    async def run_check(self) -> int:
        """Один обход: собрать, сохранить, разослать новые/перенесённые."""
        cases = await self._scraper.fetch_cases()
        to_notify: list[tuple[str, Case]] = []
        for case in cases:
            status = self.storage.upsert_case(case)
            if status in ("new", "changed"):
                to_notify.append((status, case))
        await self._notify(to_notify)
        return len(to_notify)

    async def _notify(self, items: list[tuple[str, Case]]) -> None:
        if not items:
            return
        chat_id = self.settings.effective_chat_id()
        if not chat_id:
            log.warning("ADMIN_CHAT_ID не задан — уведомления не отправлены")
            return
        if self.muted:
            log.info("Уведомления выключены — пропуск %d дел", len(items))
            return
        if len(items) > 1:
            await self._send(chat_id, f"🔔 Найдено заседаний: <b>{len(items)}</b>")
        for status, case in items:
            prefix = "🆕 " if status == "new" else "♻️ <b>Перенос заседания</b>\n"
            await self._send(chat_id, prefix + format_case(case))
            self.storage.mark_notified(case.uid)

    async def _send(self, chat_id: int, text: str) -> None:
        try:
            await self.bot.send_message(chat_id, text, disable_web_page_preview=True)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отправить сообщение в чат %s", chat_id)

    @staticmethod
    def _row_to_text(row: dict) -> str:
        parts = [f"⚖️ <b>Ст. {row.get('article') or '?'} КоАП</b>"]
        if row.get("district"):
            parts.append(f"Участок: {row['district']}")
        if row.get("case_number"):
            parts.append(f"Дело: <code>{row['case_number']}</code>")
        when = row.get("hearing_iso") or row.get("hearing_raw")
        if when:
            parts.append(f"🗓 {when}")
        return "\n".join(parts)
