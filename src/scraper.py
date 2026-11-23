"""Скрейпер ГАС «Правосудие» на Playwright (мировые судьи, поиск дел).

Навигация и заполнение формы сделаны УСТОЙЧИВО к вёрстке:
  * заходим на портал и, если нужно, переходим в раздел «Судебное делопроизводство»
    по набору известных ссылок (а не по одному жёсткому селектору);
  * форму ищем эвристически: по всем <form> на странице, поля сопоставляем по
    подписям (label/текст опций) и по именам (article/region/city/date);
  * заполняем найденные поля, отправляем ту форму, у которой есть «Найти».

Для диагностики (когда вёрстка изменилась) есть режим дампа:

    python -m src.scraper --dump --headful

Он сохраняет в logs/ HTML и скриншот каждого шага, чтобы можно было увидеть,
что реально отдаёт сайт, и поправить правила в одном месте.
"""
from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import ScraperConfig, Settings
from .parsers import Case, parse_case_html

log = logging.getLogger(__name__)

# Входная точка — главная портал ГАС «Правосудие».
HOME_URL = "https://sudrf.ru/"

# Прямой адрес раздела «Участки мировых судей» (со страницы главной).
MIR_ENTRY_URL = "https://sudrf.ru/index.php?id=300&var=true"

# Ссылки-переходы (по тексту ссылки), в порядке прохождения.
MIR_LINK_TEXTS = ("участки мировых судей",)

# Текстовые подсказки для полей формы (по подписи рядом / тексту опций).
FIELD_HINTS = {
    "article": ("статья", "статьи", "коап"),
    "region": ("регион", "субъект", "область", "край"),
    "city": ("город", "насел"),
    "court": ("суд", "участок", "мировой"),
    "date_from": ("дата", "с", "нач", "период"),
}

# Эвристики по значениям <option> для выбора нужного пункта.
OPTION_HINTS = {
    "court": ("миров",),
    "region": ("пермск",),
    "city": ("пермь",),
}


class CaptchaDetected(RuntimeError):
    """Появилась капча — обход нужно остановить."""


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
        time.sleep(random.uniform(lo, hi))

    async def _screenshot(self, page, tag: str) -> Path | None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.logs_dir / f"{tag}_{ts}.png"
        try:
            await page.screenshot(path=str(path), full_page=True)
            log.warning("Скриншот сохранён: %s", path)
            return path
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить скриншот")
            return None

    async def _dump(self, page, tag: str) -> None:
        """Сохранить HTML и скриншот страницы (для диагностики вёрстки)."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = self.logs_dir / f"{tag}_{ts}.html"
        try:
            html = await page.content()
            html_path.write_text(html, encoding="utf-8")
            log.info("HTML сохранён: %s (url=%s)", html_path, page.url)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить HTML")
        await self._screenshot(page, tag)

    async def _check_captcha(self, page) -> None:
        try:
            html = (await page.content()).lower()
        except Exception:  # noqa: BLE001
            return
        if "captcha" in html and ("введите" in html or "введите код" in html):
            await self._screenshot(page, "captcha")
            raise CaptchaDetected("Сайт просит ввести капчу")

    def _date_window(self) -> str:
        today = datetime.now()
        date_from = today - timedelta(days=self.settings.lookback_days)
        return date_from.strftime("%d.%m.%Y")

    # --------------------------------------------------------------- основной
    async def fetch_cases(
        self, articles: list[str] | None = None, dump: bool = False
    ) -> list[Case]:
        wanted = articles or self.settings.default_articles
        from playwright.async_api import async_playwright

        collected: dict[str, Case] = {}
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.cfg.headless)
            context = await browser.new_context(
                locale="ru-RU",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(self.cfg.timeout_ms)
            try:
                await self._open_search(page, dump)
                await self._fill_and_submit(page, wanted, dump)
                if dump:
                    await self._dump(page, "after_submit")
                for _ in range(self.cfg.max_pages):
                    await self._check_captcha(page)
                    html = await page.content()
                    for case in parse_case_html(html, url=page.url):
                        if not case.article or case.matches_articles(wanted):
                            collected.setdefault(case.uid, case)
                    if not await self._goto_next_page(page):
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

    # ---------------------------------------------------------- навигация
    async def _open_search(self, page, dump: bool) -> None:
        """Пройти до формы поиска дел у мировых судей.

        Путь: главная → «Участки мировых судей» → регион «Пермский край» →
        суд/участок → форма поиска (модуль судебного делопроизводства).
        На каждом шаге при --dump сохраняются HTML и скриншот, чтобы было видно,
        где именно остановились.
        """
        # 1) Открываем главную и переходим в «Участки мировых судей».
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        if dump:
            await self._dump(page, "01_home")

        if not await self._click_link_texts(page, MIR_LINK_TEXTS):
            # запасной путь — прямой адрес раздела
            log.info("Ссылка «Участки мировых судей» не найдена, открываем %s", MIR_ENTRY_URL)
            await page.goto(MIR_ENTRY_URL, wait_until="domcontentloaded")
        if dump:
            await self._dump(page, "02_mir_regions")

        # 2) Выбираем регион (по ссылке или в выпадающем списке).
        self._pause()
        await self._choose_region(page, self.settings.region)
        if dump:
            await self._dump(page, "03_perm_region")

        # 3) Переходим к судебному делопроизводству выбранного суда/участка.
        self._pause()
        await self._open_case_search_from_region(page)
        if dump:
            await self._dump(page, "04_case_search")

        # 4) Если форма уже есть — готово; иначе последняя попытка найти ссылку.
        if await self._page_has_form(page):
            return
        if await self._click_link_texts(page, ("судебное делопроизводство", "поиск дел")):
            if dump:
                await self._dump(page, "05_search_form")
            if await self._page_has_form(page):
                return

        if dump:
            await self._dump(page, "no_form")
        raise RuntimeError(
            "Не нашли форму поиска дел у мировых судей. "
            "Пришлите файлы из logs/ (0*.html) — подстроим навигацию под текущую вёрстку."
        )

    async def _page_has_form(self, page) -> bool:
        try:
            return await page.locator("form").count() > 0
        except Exception:  # noqa: BLE001
            return False

    async def _click_link_texts(self, page, texts: tuple[str, ...]) -> bool:
        """Кликнуть по первой ссылке, текст которой содержит один из фрагментов."""
        for text in texts:
            try:
                link = page.get_by_role("link", name=text, exact=False).first
                if await link.count() > 0:
                    self._pause()
                    try:
                        await link.click()
                    except Exception:  # noqa: BLE001
                        await link.click(force=True)
                    await page.wait_for_load_state("domcontentloaded")
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _choose_region(self, page, region: str) -> None:
        """Выбрать регион «Пермский край» на странице списка субъектов."""
        # вариант A: ссылка с названием региона
        if await self._click_link_texts(page, (region.lower(), "пермск")):
            return
        # вариант B: выпадающий список субъектов
        for select in await page.locator("select").all():
            if await self._select_option_by_hint(select, ("пермск",)):
                try:
                    await select.evaluate(
                        "el => el.form && el.form.submit ? el.form.submit() : null"
                    )
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await page.wait_for_load_state("domcontentloaded")
                except Exception:  # noqa: BLE001
                    pass
                return
        log.warning("Не удалось выбрать регион '%s'", region)

    async def _open_case_search_from_region(self, page) -> None:
        """На странице региона/суда перейти к судебному делопроизводству."""
        # 1) сразу ссылка «Судебное делопроизводство»
        if await self._click_link_texts(page, ("судебное делопроизводство", "поиск дел")):
            return
        # 2) иначе — первый суд/участок в списке, затем поиск
        try:
            court_link = page.locator(
                "a[href*='sud_delo'], a[href*='srv_num'], a[href*='court'], "
                "table a"
            ).first
            if await court_link.count() > 0:
                self._pause()
                await court_link.click()
                await page.wait_for_load_state("domcontentloaded")
                await self._click_link_texts(page, ("судебное делопроизводство", "поиск дел"))
        except Exception:  # noqa: BLE001
            log.warning("Не удалось открыть страницу суда/участка")

    # ------------------------------------------------------- форма поиска
    async def _fill_and_submit(self, page, articles: list[str], dump: bool) -> None:
        """Эвристически заполнить поля поиска и отправить форму."""
        values = {
            "article": articles[0],
            "region": self.settings.region,
            "city": self.settings.city,
            "court": self.settings.court_type,
            "date_from": self._date_window(),
        }

        filled_any = False
        # SELECT-поля: выбираем нужную опцию по тексту
        for select in await page.locator("select").all():
            key = await self._select_key(select)
            if not key:
                continue
            hint = OPTION_HINTS.get(key)
            if hint and await self._select_option_by_hint(select, hint):
                filled_any = True
                log.info("Выбрано поле '%s' = %s", key, hint)
            elif key in ("region", "city", "court"):
                target = values.get(key, "")
                if target and await self._select_option_by_text(select, target):
                    filled_any = True
                    log.info("Выбрано поле '%s' = %s", key, target)

        # INPUT-поля: статья и дата
        for inp in await page.locator("input").all():
            itype = (await inp.get_attribute("type") or "text").lower()
            if itype in ("submit", "button", "hidden", "checkbox", "radio", "file", "image"):
                continue
            key = await self._input_key(inp)
            if key == "article" and values["article"]:
                try:
                    await inp.fill(values["article"])
                    filled_any = True
                    log.info("Заполнена статья: %s", values["article"])
                except Exception:  # noqa: BLE001
                    pass
            elif key == "date_from" and values["date_from"]:
                try:
                    await inp.fill(values["date_from"])
                    filled_any = True
                    log.info("Заполнена дата с: %s", values["date_from"])
                except Exception:  # noqa: BLE001
                    pass

        if dump:
            await self._dump(page, "filled")

        submit = await self._find_submit(page)
        if submit is None:
            raise RuntimeError(
                "Не нашли кнопку «Найти» в форме поиска. "
                "Запустите 'python -m src.scraper --dump --headful' и пришлите logs/."
            )
        if not filled_any:
            log.warning("Не удалось распознать поля формы — отправляем как есть")
        self._pause()
        try:
            await submit.click()
        except Exception:  # noqa: BLE001
            await submit.press("Enter")
        await page.wait_for_load_state("domcontentloaded")

    async def _select_key(self, select) -> str | None:
        """Определить назначение <select> по подписи/опциям/имени."""
        try:
            name = (await select.get_attribute("name") or "").lower()
            ident = (await select.get_attribute("id") or "").lower()
            options_text = " ".join(
                [(await o.inner_text()).lower() for o in await select.locator("option").all()]
            )
        except Exception:  # noqa: BLE001
            return None
        haystack = f"{name} {ident} {options_text}"
        for key, hints in FIELD_HINTS.items():
            if any(h in haystack for h in hints):
                return key
        # по значениям опций
        if "пермск" in options_text:
            return "region"
        if "пермь" in options_text:
            return "city"
        if "миров" in options_text:
            return "court"
        return None

    async def _select_option_by_hint(self, select, hints: tuple[str, ...]) -> bool:
        options = await select.locator("option").all()
        for opt in options:
            try:
                text = (await opt.inner_text()).lower()
                value = (await opt.get_attribute("value")) or ""
            except Exception:  # noqa: BLE001
                continue
            if value == "":
                continue
            if any(h in text for h in hints):
                try:
                    await select.select_option(value=value)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    async def _select_option_by_text(self, select, target: str) -> bool:
        try:
            await select.select_option(label=target)
            return True
        except Exception:  # noqa: BLE001
            pass
        t = target.lower()
        return await self._select_option_by_hint(select, (t,))

    async def _input_key(self, inp) -> str | None:
        try:
            name = (await inp.get_attribute("name") or "").lower()
            ident = (await inp.get_attribute("id") or "").lower()
            placeholder = (await inp.get_attribute("placeholder") or "").lower()
        except Exception:  # noqa: BLE001
            return None
        haystack = f"{name} {ident} {placeholder}"
        if any(k in haystack for k in ("article", "koap", "стать")):
            return "article"
        if any(k in haystack for k in ("date", "дат")):
            return "date_from"
        return None

    async def _find_submit(self, page):
        for sel in (
            'input[type="submit"]',
            'button[type="submit"]',
            'button',
            'input[type="button"]',
            'a[role="button"]',
        ):
            try:
                loc = page.locator(sel)
                count = await loc.count()
            except Exception:  # noqa: BLE001
                continue
            for i in range(count):
                el = loc.nth(i)
                try:
                    text = ((await el.inner_text()) or "").lower()
                    value = ((await el.get_attribute("value")) or "").lower()
                except Exception:  # noqa: BLE001
                    continue
                if any(k in text or k in value for k in ("найти", "поиск", "искать", "search")):
                    return el
        # запасной вариант — первая кнопка submit
        loc = page.locator('input[type="submit"], button[type="submit"]')
        if await loc.count() > 0:
            return loc.first
        return None

    async def _goto_next_page(self, page) -> bool:
        for text in ("следующая", "далее", "»"):
            try:
                link = page.get_by_role("link", name=text).first
                if await link.count() > 0:
                    self._pause()
                    await link.click()
                    await page.wait_for_load_state("domcontentloaded")
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False


async def _run_debug(headful: bool = False, articles: list[str] | None = None, dump: bool = False) -> int:
    from .config import load_settings

    settings = load_settings()
    settings.scraper.headless = not headful
    scraper = Scraper(settings)
    try:
        cases = await scraper.fetch_cases(articles, dump=dump)
    except CaptchaDetected as exc:
        print(f"[!] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Не удалось собрать дела: {exc}")
        print("    Диагностика: python -m src.scraper --dump --headful")
        return 1
    print(f"Найдено дел: {len(cases)}")
    for c in cases:
        print(f"  - {c.article} | {c.district} | {c.case_number} | {c.hearing.iso or c.hearing.raw} | {c.url}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Отладочный запуск скрейпера ГАС «Правосудие»")
    parser.add_argument("--debug", action="store_true", help="подробные логи")
    parser.add_argument("--headful", action="store_true", help="показать окно браузера")
    parser.add_argument("--dump", action="store_true", help="сохранять HTML и скриншоты шагов в logs/")
    parser.add_argument("--articles", help="статьи через запятую, напр. '12.8,20.20'")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from .config import parse_articles

    articles = parse_articles(args.articles) if args.articles else None
    import asyncio

    return asyncio.run(_run_debug(headful=args.headful, articles=articles, dump=args.dump))


if __name__ == "__main__":
    sys.exit(main())
