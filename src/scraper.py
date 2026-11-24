"""Скрейпер ГАС «Правосудие» на Playwright (мировые судьи, поиск дел).

Схема работы:
  1. Портал sudrf.ru → «Участки мировых судей» → в справочнике выбираем
     субъект РФ (#ms_subj, напр. «Пермский край») → «Найти»;
  2. Со страницы списка собираем ссылки на сайты судебных участков
     (вида NN.perm.msudrf.ru);
  3. По каждому участку открываем /modules.php?name=sud_delo&op=hl,
     при необходимости проходим капчу (ручной ввод в видимом браузере),
     заполняем период и собираем дела, оставляя только подходящие
     по статьям КоАП.

Поля формы и кнопки ищутся эвристически (по именам/подписям), ссылки на
участки — по домену msudrf.ru. Это делает код устойчивым к смене вёрстки.

Для диагностики (когда вёрстка изменилась) есть режим дампа:

    python -m src.scraper --dump --headful

Он сохраняет в logs/ HTML и скриншот каждого шага, чтобы можно было увидеть,
что реально отдаёт сайт, и поправить правила в одном месте.
"""
from __future__ import annotations

import argparse
import asyncio
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


def _clean_ocr_code(raw: str | None) -> str:
    """Оставить в распознанном коде капчи только буквы и цифры."""
    return "".join(ch for ch in (raw or "") if ch.isalnum())


def _html_has_captcha(html: str | None) -> bool:
    """Есть ли в готовой HTML-разметке элементы формы KCAPTCHA.

    Используется для надёжной проверки во время навигации, когда DOM
    страницы пересоздаётся и локаторы Playwright временно не находят
    элементов (то падает, то ложно возвращает «капчи нет»).
    """
    if not html:
        return False
    low = html.lower()
    markers = (
        'id="kcaptchaform"',
        "id='kcaptchaform'",
        'name="captcha-response"',
        "name='captcha-response'",
    )
    if any(m in low for m in markers):
        return True
    return "captcha.php" in low and "<img" in low


def _prepare_captcha_variants(img_bytes: bytes) -> list[bytes]:
    """Дать OCR несколько предобработанных вариантов картинки KCAPTCHA.

    KCAPTCHA рисует символы с волнами, шумом и линиями. ddddocr на «сырой»
    картинке читает плохо, поэтому готовим варианты: серый + бинаризация
    (Оцу), инверсия, увеличение, фильтрация шума. OCR прогоняем по всем и
    берём самый частый результат — так точность заметно выше.
    """
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return [img_bytes]

    arr = np.frombuffer(img_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return [img_bytes]

    variants: list[bytes] = []

    def _encode(mat) -> bytes | None:
        ok, buf = cv2.imencode(".png", mat)
        return buf.tobytes() if ok else None

    # 1) апскейл x2 + серый
    up = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)

    # 2) бинаризация Оцу (тёмные символы на светлом)
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # лёгкое размытие убирает точки-шум, оставляя штрихи
    otsu_clean = cv2.medianBlur(otsu, 3)

    # 3) инверсия Оцу (на случай светлых символов)
    inv = cv2.bitwise_not(otsu_clean)

    # 4) адаптивная бинаризация (устойчива к градиенту фона)
    adaptive = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
    )

    # 5) просто увеличенное серое
    for mat in (gray, otsu, otsu_clean, inv, adaptive):
        enc = _encode(mat)
        if enc:
            variants.append(enc)
    return variants or [img_bytes]


def _vote_codes(codes: list[str]) -> str:
    """Выбрать самый частый код среди прочтений (по среднему при равенстве).

    Несколько предобработанных вариантов и повторные прогоны дают набор
    гипотез. Берём самый частый; при равенстве — самый длинный (KCAPTCHA
    обычно 5–6 символов, обрезанное чтение короче).
    """
    from collections import Counter  # noqa: PLC0415

    cleaned = [_clean_ocr_code(c) for c in codes]
    cleaned = [c for c in cleaned if c]
    if not cleaned:
        return ""
    counts = Counter(cleaned)
    best_count = max(counts.values())
    candidates = [c for c, n in counts.items() if n == best_count]
    candidates.sort(key=len, reverse=True)
    return candidates[0]


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

    def _date_window(self) -> str:
        today = datetime.now()
        date_from = today - timedelta(days=self.settings.lookback_days)
        return date_from.strftime("%d.%m.%Y")

    def _date_window_to(self) -> str:
        return datetime.now().strftime("%d.%m.%Y")

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
                ignore_https_errors=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(self.cfg.timeout_ms)
            try:
                # 1) справочник участков → список участков региона
                await self._open_precinct_list(page, dump)
                precinct_urls = await self._collect_precinct_urls(page)
                log.info("Найдено участков региона: %d", len(precinct_urls))
                if dump:
                    await self._dump(page, "05_precinct_urls")
                if not precinct_urls:
                    raise RuntimeError(
                        "Не удалось получить список участков со страницы справочника. "
                        "Пришлите logs/04_precinct_list_*.html — подстроим разбор ссылок."
                    )

                # 2) по каждому участку — форма делопроизводства и сбор дел
                blocked = 0
                checked = 0
                for idx, site_url in enumerate(precinct_urls, 1):
                    log.info("[%d/%d] Участок: %s", idx, len(precinct_urls), site_url)
                    checked += 1
                    try:
                        ok = await self._open_case_search_on_site(page, site_url, dump)
                    except CaptchaDetected:
                        raise
                    except Exception:  # noqa: BLE001
                        log.warning("Пропускаю участок %s (не открылся)", site_url)
                        continue
                    if not ok:
                        if await self._is_blocked(page):
                            blocked += 1
                        log.warning("Не нашли форму дел на %s", site_url)
                        if dump:
                            await self._dump(page, f"no_form_{idx:02d}")
                        continue
                    # капча могла появиться/не пройти на шаге открытия сайта
                    if await self._has_captcha(page):
                        await self._pass_captcha(page, dump)
                    if await self._has_captcha(page):
                        log.warning("Капча на %s не пройдена, пропускаю участок", site_url)
                        if dump:
                            await self._dump(page, f"captcha_skip_{idx:02d}")
                        continue
                    # после капчи страница может быть пустым JS-скелетом:
                    # ждём, пока отрисуется форма дел, иначе капча вернётся снова
                    if not await self._wait_for_case_form(page, timeout_ms=15000):
                        log.warning("Форма дел на %s не отрисовалась вовремя", site_url)
                        if dump:
                            await self._dump(page, f"form_timeout_{idx:02d}")
                        continue
                    if await self._has_captcha(page):
                        await self._pass_captcha(page, dump)
                        if await self._has_captcha(page):
                            log.warning("Капча на %s не пройдена, пропускаю участок", site_url)
                            continue
                    try:
                        await self._fill_and_submit(page, wanted, dump)
                    except Exception:  # noqa: BLE001
                        log.warning("Не удалось отправить форму на %s", site_url)
                        continue
                    if dump:
                        await self._dump(page, f"after_submit_{idx:02d}")
                    for _ in range(self.cfg.max_pages):
                        if await self._has_captcha(page):
                            await self._pass_captcha(page, dump)
                        html = await page.content()
                        for case in parse_case_html(html, url=page.url):
                            if not case.article or case.matches_articles(wanted):
                                collected.setdefault(case.uid, case)
                        if not await self._goto_next_page(page):
                            break
                    self._pause()
                if checked and blocked >= checked:
                    raise RuntimeError(
                        "Все сайты участков ответили 403 «Доступ запрещен». "
                        "Портал блокирует автоматические запросы с этого IP. "
                        "Запустите с видимым браузером (headless: false в config.yaml "
                        "или флаг --headful), смените сеть/VPN или выждите немного — "
                        "и попробуйте снова."
                    )
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
    async def _open_precinct_list(self, page, dump: bool) -> None:
        """Открыть справочник участков и получить список участков региона.

        Путь: главная → «Участки мировых судей» → выбрать субъект РФ
        (#ms_subj, Пермский край) → «Найти». Результат — страница списка
        судебных участков выбранного региона.
        """
        await page.goto(HOME_URL, wait_until="domcontentloaded")
        if dump:
            await self._dump(page, "01_home")

        if not await self._click_link_texts(page, MIR_LINK_TEXTS):
            log.info("Ссылка «Участки мировых судей» не найдена, открываем %s", MIR_ENTRY_URL)
            await page.goto(MIR_ENTRY_URL, wait_until="domcontentloaded")
        if dump:
            await self._dump(page, "02_mir_regions")

        # Форма справочника участков: #ms_court_search, submit value="Найти".
        try:
            await page.locator("#ms_subj").select_option(label=self.settings.region)
        except Exception:  # noqa: BLE001
            # запасной путь — выбрать опцию по тексту
            sel = page.locator("#ms_subj")
            if await sel.count() == 0:
                raise RuntimeError(
                    "Не нашли форму справочника участков (#ms_court_search / #ms_subj). "
                    "Пришлите logs/02_mir_regions_*.html — подстроим селекторы."
                ) from None
            await self._select_option_by_hint(sel.first, ("пермск",))
        # убеждаемся, что выбран тип «участок мирового судьи»
        try:
            await page.locator("#msSearchType-ms").check()
        except Exception:  # noqa: BLE001
            pass
        if dump:
            await self._dump(page, "03_perm_selected")

        self._pause()
        submit = page.locator('#ms_court_search input[type="submit"]').first
        if await submit.count() == 0:
            submit = page.get_by_role("button", name="Найти").first
        await submit.click()
        await page.wait_for_load_state("domcontentloaded")
        if dump:
            await self._dump(page, "04_precinct_list")

    async def _collect_precinct_urls(self, page) -> list[str]:
        """Собрать абсолютные ссылки на сайты участков со страницы списка."""
        from urllib.parse import urljoin, urlparse

        urls: list[str] = []
        seen: set[str] = set()
        try:
            anchors = await page.locator("a[href]").all()
        except Exception:  # noqa: BLE001
            return urls
        for a in anchors:
            try:
                href = await a.get_attribute("href") or ""
            except Exception:  # noqa: BLE001
                continue
            if not href or href.startswith("#") or href.lower().startswith("javascript"):
                continue
            absolute = urljoin(page.url, href)
            # интересуют только сайты участков вида NN.<region>.msudrf.ru
            host = urlparse(absolute).hostname or ""
            if not host.endswith(".msudrf.ru"):
                continue
            if any(k in absolute for k in ("index.php", "counter", "yandex", "/themes/")):
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            urls.append(absolute)
        return urls

    async def _is_blocked(self, page) -> bool:
        """Страница отдала 403/«Доступ запрещен» (антибот на сайтах участков)."""
        try:
            html = (await page.content()).lower()
        except Exception:  # noqa: BLE001
            return False
        markers = ("доступ запрещен", "доступ запрещён", "403 forbidden", "error 403")
        return any(m in html for m in markers)

    async def _open_site_with_retry(self, page, site_url: str, dump: bool) -> bool:
        """Открыть главную сайта участка, при блокировке — пауза и повтор."""
        for attempt in (1, 2):
            try:
                await page.goto(site_url, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                log.warning("Не открылся %s (попытка %d): %s", site_url, attempt, exc)
                self._pause()
                continue
            if not await self._is_blocked(page):
                return True
            log.warning("403 на %s (попытка %d)", site_url, attempt)
            if dump and attempt == 1:
                await self._dump(page, "blocked_site")
            self._pause()
        return False

    async def _open_case_search_on_site(self, page, site_url: str, dump: bool) -> bool:
        """На сайте участка открыть модуль судебного делопроизводства (op=hl)."""
        if not await self._open_site_with_retry(page, site_url, dump):
            if dump:
                await self._dump(page, "site_no_form")
            return False
        # настоящий адрес модуля дел: /modules.php?name=sud_delo&op=hl
        delo_url = site_url.rstrip("/") + "/modules.php?name=sud_delo&op=hl"
        try:
            await page.goto(delo_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            log.warning("Не открылся модуль дел на %s: %s", site_url, exc)
            return False
        if await self._is_blocked(page):
            log.warning("403 на модуле дел %s", site_url)
            return False
        # модуль может сразу выдать капчу («дополнительную проверку»)
        if await self._has_captcha(page):
            await self._pass_captcha(page, dump)
        if await self._page_has_form(page):
            return True
        await self._pause()
        if await self._click_link_texts(
            page, ("судебное делопроизводство", "поиск дел", "назначенные к рассмотрению")
        ):
            if await self._has_captcha(page):
                await self._pass_captcha(page, dump)
            return await self._page_has_form(page)
        if dump:
            await self._dump(page, "site_no_form")
        return False

    async def _has_captcha(self, page) -> bool:
        """Капча определяется только по реальным элементам формы ввода.

        Не полагаемся на текст «дополнительную проверку» или строку
        "/captcha.php": после успешного ввода они могут остаться в разметке
        (ссылки, скрипты) и бесконечно зацикливали ожидание.

        Во время навигации DOM пересоздаётся: локаторы могут временно падать
        или ничего не находить. Тогда подстраховываемся разбором HTML
        (page.content()), чтобы не принять промежуточное состояние за успех.
        """
        found = False
        try:
            if await page.locator("#kcaptchaForm").count() > 0:
                found = True
            elif await page.locator('input[name="captcha-response"]').count() > 0:
                found = True
            elif await page.locator('img[src*="captcha"], img[src*="kcaptcha"]').count() > 0:
                found = True
            else:
                return False
        except Exception:  # noqa: BLE001
            pass
        if found:
            return True
        # локаторы не нашли — проверяем по HTML (устойчиво к навигации)
        try:
            return _html_has_captcha(await page.content())
        except Exception:  # noqa: BLE001
            return False

    async def _pass_captcha(self, page, dump: bool) -> None:
        """Пройти капчу: авто (ddddocr) с ретраями либо ручной ввод."""
        await self._screenshot(page, "captcha")
        if self.cfg.captcha_mode == "auto":
            try:
                if await self._auto_solve_captcha(page, dump):
                    log.info("Капча решена автоматически")
                    return
            except CaptchaDetected:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("Авто-решение капчи не удалось (%s), переходим к ручному вводу", exc)
        await self._manual_captcha(page, dump)

    async def _auto_solve_captcha(self, page, dump: bool) -> bool:
        """Распознать KCAPTCHA: сначала локально (ddddocr), затем vision-модель.

        Локальный OCR бесплатный и быстрый, поэтому пробуем его первым. Если
        он недоступен, несколько раз подряд не дал уверенного кода или сервер
        отверг распознанное — подключаем vision-модель (если задан ключ).
        """
        ocr = None
        try:
            import ddddocr  # noqa: PLC0415

            ocr = ddddocr.DdddOcr(show_ad=False)
        except Exception:  # noqa: BLE001
            log.info("ddddocr недоступен — полагаемся на vision-модель")
        if not await self._has_captcha(page):
            return True

        vision_key = getattr(self.settings, "vision_api_key", "")
        vision_allowed = self.cfg.captcha_vision_enabled and bool(vision_key)

        attempts = max(1, self.cfg.captcha_max_attempts)
        local_failures = 0
        for attempt in range(1, attempts + 1):
            img_loc = page.locator('img[src*="captcha"], img[src*="kcaptcha"]').first
            if await img_loc.count() == 0:
                # нет картинки — возможно, капча уже пройдена
                if not await self._has_captcha(page):
                    return True
                return False
            try:
                img_bytes = await self._fetch_captcha_image(page, img_loc)
            except Exception:  # noqa: BLE001
                return False

            code = ""
            source = "ocr"
            if ocr is not None and local_failures < 2:
                code = self._local_ocr(ocr, img_bytes, attempt, attempts)
            if not code and vision_allowed:
                source = "vision"
                for cand in self._vision_ocr(img_bytes):
                    if await self._try_captcha_code(page, cand, dump):
                        return True
                # vision не помог — одна неудачная попытка, идём дальше
                local_failures += 1
                self._pause()
                continue
            if not code:
                # локальный OCR пуст и vision недоступен — перерисовать капчу
                if ocr is not None:
                    local_failures += 1
                await self._reload_captcha(page)
                continue

            if await self._try_captcha_code(page, code, dump):
                return True
            log.info("Капча не принята (код «%s», %s), пробуем снова", code, source)
            local_failures += 1
            self._pause()
        return False

    def _local_ocr(self, ocr, img_bytes: bytes, attempt: int, attempts: int) -> str:
        """Локальное распознавание с предобработкой и голосованием вариантов."""
        try:
            variants = _prepare_captcha_variants(img_bytes)
            codes: list[str] = []
            for var in variants:
                try:
                    codes.append(_clean_ocr_code(ocr.classification(var)))
                except Exception:  # noqa: BLE001
                    continue
            code = _vote_codes(codes)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR капчи упал: %s", exc)
            return ""
        log.info(
            "Капча, попытка %d/%d: варианты=%s → «%s»",
            attempt,
            attempts,
            [c for c in codes if c],
            code,
        )
        return code

    def _vision_ocr(self, img_bytes: bytes) -> list[str]:
        """Спросить vision-модель; вернуть список кандидатов (возможно пустой)."""
        try:
            from .captcha_ai import solve_captcha_vision  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return []
        code = solve_captcha_vision(
            img_bytes,
            api_key=getattr(self.settings, "vision_api_key", ""),
            model=self.cfg.captcha_vision_model,
            base_url=self.cfg.captcha_vision_base_url,
            auth_scheme=self.cfg.captcha_vision_auth_scheme,
            folder_id=self.cfg.captcha_vision_folder_id,
        )
        if not code:
            return []
        # иногда модели возвращают и заглавные, и в нижнем регистре — пробуем оба
        variants = [code]
        if code.lower() != code:
            variants.append(code.lower())
        if code.upper() != code:
            variants.append(code.upper())
        return variants

    async def _try_captcha_code(self, page, code: str, dump: bool) -> bool:
        """Ввести код капчи, отправить и проверить, принят ли."""
        if not code:
            return False
        inp = page.locator('input[name="captcha-response"]').first
        if await inp.count() == 0:
            return False
        try:
            await inp.fill(code)
        except Exception:  # noqa: BLE001
            return False
        await self._submit_captcha(page)
        ok = await self._wait_captcha_gone(page, timeout_ms=8000)
        if ok:
            # форма дел дорисовывается JS уже после капчи — дождёмся её,
            # иначе сразу после «после капчи» страница ещё пустой скелет
            await self._wait_for_case_form(page, timeout_ms=15000)
            if dump:
                await self._dump(page, "after_captcha")
            return True
        return False

    async def _fetch_captcha_image(self, page, img_loc) -> bytes:
        """Получить PNG/JPEG байты картинки капчи.

        Предпочтительно скачать по src через контекст запросов браузера —
        так не теряется резкость из-за масштабирования элемента. Если не
        удалось — снимаем скриншот самого элемента.
        """
        src = None
        try:
            src = await img_loc.get_attribute("src")
        except Exception:  # noqa: BLE001
            src = None
        if src:
            try:
                from urllib.parse import urljoin  # noqa: PLC0415

                url = urljoin(page.url, src)
                resp = await page.context.request.get(url)
                if resp.ok:
                    data = await resp.body()
                    if data:
                        return data
            except Exception:  # noqa: BLE001
                pass
        return await img_loc.screenshot()

    async def _submit_captcha(self, page) -> None:
        form = page.locator("#kcaptchaForm").first
        if await form.count() > 0:
            for sel in ('input[type="submit"]', 'button[type="submit"]', "button"):
                btn = form.locator(sel).first
                try:
                    if await btn.count() > 0:
                        await btn.click()
                        return
                except Exception:  # noqa: BLE001
                    continue
        # запасной путь: Enter в поле
        inp = page.locator('input[name="captcha-response"]').first
        try:
            if await inp.count() > 0:
                await inp.press("Enter")
                return
        except Exception:  # noqa: BLE001
            pass
        # последняя надежда — любая кнопка «Продолжить»
        try:
            await page.get_by_role("button", name="Продолжить").first.click()
        except Exception:  # noqa: BLE001
            pass

    async def _wait_captcha_gone(self, page, timeout_ms: int) -> bool:
        """Дождаться, пока форма капчи исчезнет после отправки.

        Требуем два подряд подтверждения «капчи нет» с паузой: при отправке
        страница перезагружается, и одиночная проверка могла застать момент,
        когда старый DOM уже открепился, а новый ещё не отрисовался — это
        ложно трактовалось как «капча пройдена».
        """
        return await self._poll_until(page, lambda: self._captcha_absent(page), timeout_ms)

    async def _captcha_absent(self, page) -> bool:
        return not await self._has_captcha(page)

    async def _poll_until(self, page, predicate, timeout_ms: int) -> bool:
        """Ждать, пока ``predicate(page)`` вернёт True два раза подряд.

        ``predicate`` — корутинная функция, уже привязанная к ``page``; параметр
        ``page`` оставлен для единообразия вызовов и в проверку не передаётся.

        Двойное подтверждение с паузой защищает от ложного срабатывания в
        момент перезагрузки/перерисовки страницы (старый DOM открепился, новый
        ещё не отрисован).
        """
        step_ms = 500
        waited = 0
        misses = 0
        while True:
            try:
                ok = bool(await predicate())
            except Exception:  # noqa: BLE001
                ok = False
            misses = misses + 1 if ok else 0
            if misses >= 2:
                return True
            if waited >= timeout_ms:
                return False
            await asyncio.sleep(step_ms / 1000)
            waited += step_ms

    async def _wait_for_case_form(self, page, timeout_ms: int) -> bool:
        """Дождаться, пока страница участка отрисует форму дел после капчи.

        После прохождения капчи сервер отдаёт «скелет» страницы: ``<body>``
        пуст, разметку дорисовывает JS. Если сразу лезть в форму, её ещё нет,
        и сайт снова требует капчу. Поэтому ждём появления характерных узлов:
        вкладки административных дел, контента ``#bookmark_type_2``, поля
        статьи или блока кнопок поиска.
        """
        return await self._poll_until(page, lambda: self._case_form_ready(page), timeout_ms)

    async def _case_form_ready(self, page) -> bool:
        try:
            for sel in (
                "#bookmark_type_2",
                "li#type_2",
                "#button_block",
                'input[name="ADM_PARTS__LAW_ARTICLE"]',
            ):
                if await page.locator(sel).count() > 0:
                    return True
        except Exception:  # noqa: BLE001
            return False
        return False

    async def _reload_captcha(self, page) -> None:
        """Обновить картинку капчи (клик по ней или перезагрузка страницы)."""
        img_loc = page.locator('img[src*="captcha"], img[src*="kcaptcha"]').first
        try:
            if await img_loc.count() > 0:
                await img_loc.click()
                await asyncio.sleep(0.5)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            await page.reload(wait_until="domcontentloaded")
        except Exception:  # noqa: BLE001
            pass

    async def _manual_captcha(self, page, dump: bool) -> None:
        """Ввести капчу вручную в окне браузера и дождаться продолжения."""
        if self.cfg.headless:
            raise CaptchaDetected(
                "Сайт участка просит капчу. Запустите в видимом браузере "
                "(headless: false в config.yaml) — тогда капчу можно будет ввести вручную."
            )
        log.warning(
            "=== ВВЕДИТЕ КАПЧУ В ОКНЕ БРАУЗЕРА и нажмите «Продолжить» (ждём до %d с) ===",
            self.cfg.captcha_wait_seconds,
        )
        deadline = self.cfg.captcha_wait_seconds
        waited = 0.0
        step = 1.0
        while waited < deadline:
            if not await self._has_captcha(page):
                log.info("Капча пройдена, продолжаем")
                if dump:
                    await self._dump(page, "after_captcha")
                return
            await asyncio.sleep(step)
            waited += step
        await self._screenshot(page, "captcha_timeout")
        raise CaptchaDetected(
            "Капча не введена за отведённое время. Увеличьте captcha_wait_seconds "
            "в config.yaml или введите код быстрее."
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

    # ------------------------------------------------------- форма поиска
    async def _fill_and_submit(self, page, articles: list[str], dump: bool) -> None:
        """Заполнить форму делопроизводства участка (вкладка КоАП) и искать.

        Реальная вёрстка /modules.php?name=sud_delo&op=hl:
          * вкладки типов дел (.bookmarks li#type_2 — «Дела об
            административных правонарушениях»), контент в #bookmark_type_2;
          * поля: ``adm_case__CASE_NUMBERSS``, ``ADM_PARTS__LAW_ARTICLE``
            (главная статья КоАП), даты ``adm_case__RESULT_DATE1D/2D``;
          * кнопка «Искать» внутри #button_block, результат в #search_results.
        Статья — это autocomplete-поле .select-values; проще всего ввести текст
        статьи и подтвердить значение.
        """
        # форма могла ещё не отрисоваться после капчи (JS-скелет) — подождём
        await self._wait_for_case_form(page, timeout_ms=15000)
        await self._open_admin_tab(page)

        date_from = self._date_window()
        date_to = self._date_window_to()
        filled_article = False
        for art in articles:
            if await self._fill_admin_article(page, art):
                filled_article = True
                log.info("Заполнена статья КоАП: %s", art)
                break
        if not filled_article:
            log.warning("Не удалось заполнить статью КоАП в форме участка")

        await self._fill_date(page, "adm_case__RESULT_DATE1D", date_from)
        await self._fill_date(page, "adm_case__RESULT_DATE2D", date_to)

        if dump:
            await self._dump(page, "filled")

        submit = page.locator("#button_block input.search").first
        if await submit.count() == 0:
            submit = await self._find_submit(page)
        if submit is None:
            raise RuntimeError(
                "Не нашли кнопку «Искать» в форме дел участка. "
                "Запустите 'python -m src.scraper --dump --headful' и пришлите logs/."
            )
        self._pause()
        try:
            await submit.click()
        except Exception:  # noqa: BLE001
            await submit.press("Enter")
        # результат подгружается AJAX-ом в #search_results
        await self._wait_search_results(page)

    async def _open_admin_tab(self, page) -> None:
        """Переключиться на вкладку «Дела об административных правонарушениях»."""
        tab = page.locator("#bookmark_type_2, li#type_2").first
        try:
            if await tab.count() > 0:
                self._pause()
                await tab.click()
                await asyncio.sleep(0.3)
                log.info("Открыта вкладка административных дел")
        except Exception:  # noqa: BLE001
            log.warning("Не удалось переключить вкладку административных дел")

    async def _fill_admin_article(self, page, article: str) -> bool:
        """Ввести статью КоАП в поле «Главная статья (КоАП, ТК)»."""
        selector = '#bookmark_type_2 input[name="ADM_PARTS__LAW_ARTICLE"]'
        inp = page.locator(selector).first
        if await inp.count() == 0:
            # запасной поиск по имени на всей странице
            inp = page.locator('input[name="ADM_PARTS__LAW_ARTICLE"]').first
            if await inp.count() == 0:
                return False
        try:
            await inp.click()
            await inp.fill("")
            await inp.type(article, delay=50)
            await asyncio.sleep(0.5)
            # autocomplete: подтверждаем первое совпадение, если есть список
            try:
                await inp.press("ArrowDown")
                await inp.press("Enter")
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _fill_date(self, page, name: str, value: str) -> None:
        if not value:
            return
        try:
            inp = page.locator(f'input[name="{name}"]').first
            if await inp.count() > 0:
                await inp.fill(value)
                log.info("Заполнено поле %s = %s", name, value)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_search_results(self, page) -> None:
        """Дождаться, пока AJAX обновит блок результатов поиска."""
        try:
            await page.wait_for_function(
                """() => {
                    const r = document.querySelector('#search_results');
                    if (!r) return true;
                    return r.querySelector('table') || r.querySelector('a[href]')
                        || /не\\s+назначено|не\\s+найден/i.test(r.textContent || '');
                }""",
                timeout=self.cfg.timeout_ms,
            )
        except Exception:  # noqa: BLE001
            # не критично: разберём текущий DOM как есть
            await asyncio.sleep(1.0)
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:  # noqa: BLE001
            pass

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

    return asyncio.run(_run_debug(headful=args.headful, articles=articles, dump=args.dump))


if __name__ == "__main__":
    sys.exit(main())
