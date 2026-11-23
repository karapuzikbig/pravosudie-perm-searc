"""Скрейпер ГАС «Правосудие» на Playwright.

Раздел «Судебное делопроизводство» / поиск дел ГАС «Правосудие» имеет
динамическую вёрстку, поэтому здесь намеренно несколько защитных стратегий:

  1) Поиск ссылки/пункта меню модуля судебного делопроизводства.
  2) Заполнение формы: тип суда, регион, город, статья, дата.
  3) Пагинация с предохранителем max_pages.
  4) При любой проблеме — лог, скриншот (logs/) и уведомление.

Скрейпер НЕ работает из песочницы без доступа к sudrf.ru. Запуск для
отладки на VPS:

    python -m src.scraper --debug --headful

селекторы централизованы в SELECTORS, чтобы их можно было править в одном
месте, не трогая логику.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .config import ScraperConfig, Settings
from .parsers import Case, parse_case_html

log = logging.getLogger(__name__)

# Базовый адрес портала ГАС «Правосудие»
BASE_URL = "https://sudrf.ru"

# Стратегии поиска ссылки на раздел «Судебное делопроизводство».
# Проверяются по очереди, пока одна не сработает.
SELECTORS: dict[str, list[str]] = {
    "case_search_link": [
        'a[href*="sud_delo"]',
        'a[href*="sudoproizvodstvo"]',
        'a[href*="delo"]',
        'a:has-text("Судебное делопроизводство")',
        'a:has-text("Поиск дел")',
    ],
    "court_type_select": [
        'select[name*="court"]',
        'select#courtType',
        'select:has(option:has-text("Мировой"))',
    ],
    "region_select": [
        'select[name*="region"]',
        'select#region',
    ],
    "city_select": [
        'select[name*="city"]',
        'select#city',
    ],
    "article_input": [
        'input[name*="article"]',
        'input[name*="koap"]',
        'input[placeholder*="тать"]',
    ],
    "date_from_input": [
        'input[name*="date_from"]',
        'input[name*="dateFrom"]',
        'input[type="date"]',
    ],
    "submit_button": [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Найти")',
        'button:has-text("Поиск")',
    ],
    "results_container": [
        "table",
        ".results",
        "#results",
    ],
    "next_page": [
        'a:has-text("Следующая")',
        'a[rel="next"]',
        ".pagination a.next",
    ],
    "captcha": [
        "img[src*='captcha']",
        "#captcha",
        ".captcha",
    ],
}


class CaptchaDetected(RuntimeError):
    """Появилась капча — обход нужно остановить и уведомить админа."""


class Scraper:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cfg: ScraperConfig = settings.scraper
        self.logs_dir = Path("logs")
        self.logs_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------ utils
    def _pause(self) -> None:
        lo = self.cfg.delay_min_between_requests
        hi = max(lo, self.cfg.delay_max_between_requests)
        import time

        time.sleep(random.uniform(lo, hi))

    async def _first(self, page, strategy_key: str):
        """Вернуть первый найденный локатор по списку селекторов."""
        for sel in SELECTORS[strategy_key]:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    return loc
            except Exception:  # noqa: BLE001 - селектор может быть невалиден
                continue
        return None

    async def _check_captcha(self, page) -> None:
        for sel in SELECTORS["captcha"]:
            try:
                if await page.locator(sel).first.count() > 0:
                    await self._screenshot(page, "captcha")
                    raise CaptchaDetected("Обнаружена капча на ГАС «Правосудие»")
            except CaptchaDetected:
                raise
            except Exception:  # noqa: BLE001
                continue

    async def _screenshot(self, page, tag: str) -> None:
        if not self.cfg.screenshot_on_error:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.logs_dir / f"{tag}_{ts}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            log.warning("Скриншот сохранён: %s", path)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить скриншот")

    def _date_window(self) -> tuple[str, str]:
        today = datetime.now()
        date_from = today - timedelta(days=self.settings.lookback_days)
        # Верхнюю границу берём с запасом — будущие заседания
        date_to = today + timedelta(days=90)
        fmt = "%d.%m.%Y"
        return date_from.strftime(fmt), date_to.strftime(fmt)

    # --------------------------------------------------------------- основной
    async def fetch_cases(self, articles: list[str] | None = None) -> list[Case]:
        """Собрать дела по указанным статьям (по умолчанию — из настроек)."""
        wanted = articles or self.settings.default_articles
        from playwright.async_api import async_playwright

        collected: dict[str, Case] = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.cfg.headless)
            context = await browser.new_context(
                locale="ru-RU",
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(self.cfg.timeout_ms)
            try:
                await self._open_search(page)
                await self._fill_form(page, wanted)
                for _ in range(self.cfg.max_pages):
                    await self._check_captcha(page)
                    html = await page.content()
                    for case in parse_case_html(html, url=page.url):
                        if not case.article or case.matches_articles(wanted):
                            collected.setdefault(case.uid, case)
                    nxt = await self._first(page, "next_page")
                    if not nxt:
                        break
                    self._pause()
                    try:
                        await nxt.click()
                        await page.wait_for_load_state("networkidle")
                    except Exception:  # noqa: BLE001
                        log.info("Пагинация завершена или недоступна")
                        break
            except CaptchaDetected:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Ошибка при сборе дел")
                await self._screenshot(page, "error")
                raise
            finally:
                await browser.close()

        log.info("Собрано уникальных дел: %d", len(collected))
        return list(collected.values())

    async def _open_search(self, page) -> None:
        await page.goto(BASE_URL, wait_until="domcontentloaded")
        link = await self._first(page, "case_search_link")
        if link is None:
            log.warning("Ссылка на судебное делопроизводство не найдена на главной")
            return
        self._pause()
        try:
            await link.click()
            await page.wait_for_load_state("networkidle")
        except Exception:  # noqa: BLE001
            log.warning("Переход по ссылке делопроизводства не удался")

    async def _fill_form(self, page, articles: list[str]) -> None:
        # тип суда
        loc = await self._first(page, "court_type_select")
        if loc:
            try:
                await loc.select_option(label=self.settings.court_type)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось выбрать тип суда '%s'", self.settings.court_type)

        # регион
        loc = await self._first(page, "region_select")
        if loc:
            try:
                await loc.select_option(label=self.settings.region)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось выбрать регион '%s'", self.settings.region)

        # город
        loc = await self._first(page, "city_select")
        if loc:
            try:
                await loc.select_option(label=self.settings.city)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось выбрать город '%s'", self.settings.city)

        # статья (первая из списка забивается в форму; остальные фильтруем
        # по результатам на нашей стороне в fetch_cases)
        loc = await self._first(page, "article_input")
        if loc:
            try:
                await loc.fill(articles[0])
            except Exception:  # noqa: BLE001
                log.warning("Не удалось заполнить статью")

        # дата
        date_from, _ = self._date_window()
        loc = await self._first(page, "date_from_input")
        if loc:
            try:
                await loc.fill(date_from)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось заполнить дату начала (%s)", date_from)

        # отправка
        submit = await self._first(page, "submit_button")
        if submit is None:
            raise RuntimeError("Кнопка поиска не найдена — вероятно, изменилась вёрстка")
        self._pause()
        await submit.click()
        await page.wait_for_load_state("networkidle")


async def _run_debug(headful: bool = False, articles: list[str] | None = None) -> int:
    from .config import load_settings

    settings = load_settings()
    settings.scraper.headless = not headful
    scraper = Scraper(settings)
    try:
        cases = await scraper.fetch_cases(articles)
    except CaptchaDetected as exc:
        print(f"[!] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Не удалось собрать дела: {exc}")
        print("    Проверьте: доступ к sudrf.ru, установлен ли браузер (playwright install chromium),")
        print("    и, при необходимости, поправьте селекторы в SELECTORS (src/scraper.py).")
        return 1
    print(f"Найдено дел: {len(cases)}")
    for c in cases:
        print(f"  - {c.article} | {c.district} | {c.case_number} | {c.hearing.iso or c.hearing.raw} | {c.url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Отладочный запуск скрейпера ГАС «Правосудие»")
    parser.add_argument("--debug", action="store_true", help="подробные логи")
    parser.add_argument("--headful", action="store_true", help="показать окно браузера")
    parser.add_argument("--articles", help="статьи через запятую, напр. '12.8,20.20'")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .config import parse_articles

    articles = parse_articles(args.articles) if args.articles else None
    import asyncio

    return asyncio.run(_run_debug(headful=args.headful, articles=articles))


if __name__ == "__main__":
    sys.exit(main())
