"""Нормализация карточек дел и судебных заседаний ГАС «Правосудие».

Модуль не ходит в сеть: на вход подаётся HTML (или уже собранные поля),
на выход — структурированные объекты Case. Это позволяет покрыть логику
юнит-тестами на сохранённых фикстурах, а скрейпер (scraper.py) остаётся
тонким слоем вокруг Playwright.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\b")
ARTICLE_RE = re.compile(r"\b(\d{1,2}\.\d{1,2}(?:\.\d{1,3})?)\s*(?:ч\.?\s*\d+)?\s*КоАП", re.IGNORECASE)
CASE_NUMBER_RE = re.compile(r"\b\d+[-\u2013]\d+/\d{4}\b")
DISTRICT_RE = re.compile(r"(судебн\w+\s+участ\w*)\s*№?\s*(\d+)", re.IGNORECASE)
HEARING_HEADER_KEYS = ("заседани", "дата", "время", "назнач")
PARTICIPANTS_HEADER_KEYS = (
    "участник",
    "лицо",
    "стороны",
    "сторона",
    "фио",
    "правонарушитель",
    "привлекаем",
    "ответчик",
    "истец",
)


@dataclass
class Hearing:
    """Одно судебное заседание (дата/время)."""

    date: datetime | None = None
    raw: str = ""
    time_raw: str = ""

    @property
    def iso(self) -> str | None:
        return self.date.strftime("%Y-%m-%d %H:%M") if self.date else None

    @property
    def date_str(self) -> str:
        """Дата заседания в виде ДД.ММ.ГГГГ (или исходный текст)."""
        if self.date:
            return self.date.strftime("%d.%m.%Y")
        m = DATE_RE.search(self.raw or "")
        return m.group(0) if m else ""

    @property
    def time_str(self) -> str:
        """Время заседания в виде ЧЧ:ММ (или пустая строка)."""
        if self.time_raw:
            return self.time_raw
        if self.date and (self.date.hour or self.date.minute):
            return self.date.strftime("%H:%M")
        m = TIME_RE.search(self.raw or "")
        return m.group(0) if m else ""


@dataclass
class Case:
    """Нормализованная карточка дела."""

    case_number: str
    article: str
    court: str = ""
    district: str = ""
    hearing: Hearing = field(default_factory=Hearing)
    url: str = ""
    summary: str = ""
    participants: str = ""

    @property
    def uid(self) -> str:
        """Стабильный идентификатор для антидублей."""
        key = self.case_number or f"{self.district}|{self.article}|{self.hearing.raw}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]

    @property
    def dedup_key(self) -> str:
        """Ключ антидублей для Excel: номер дела + дата + время (+ участники).

        Одно дело может заседательствовать несколько раз, поэтому номер дела
        сам по себе слишком грубый ключ — учитываем дату/время заседания.
        """
        parts = "|".join(
            [
                self.case_number or "",
                self.hearing.date_str or "",
                self.hearing.time_str or "",
                (self.participants or "").lower(),
            ]
        )
        return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]

    def matches_articles(self, articles: Iterable[str]) -> bool:
        art = (self.article or "").strip()
        return any(art == a or art.startswith(a + ".") or art.startswith(a + " ") for a in articles)


def parse_date(text: str) -> datetime | None:
    """Извлечь дату (и время, если есть) из строки вида '12.03.2025 14:30'."""
    if not text:
        return None
    m = DATE_RE.search(text)
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    hour = minute = 0
    tm = TIME_RE.search(text)
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        if hour > 23 or minute > 59:
            hour = minute = 0
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def extract_article(text: str) -> str:
    m = ARTICLE_RE.search(text or "")
    if m:
        return m.group(1)
    m = re.search(r"\b(\d{1,2}\.\d{1,2})\b", text or "")
    return m.group(1) if m else ""


def extract_case_number(text: str) -> str:
    m = CASE_NUMBER_RE.search(text or "")
    return m.group(0).replace("\u2013", "-") if m else ""


def extract_district(text: str) -> str:
    m = DISTRICT_RE.search(text or "")
    if m:
        return f"Судебный участок №{m.group(2)}"
    return ""


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _split_participants(text: str) -> str:
    """Собрать участников дела в читаемую строку через «; ».

    В ячейке участники обычно идут списком через запятую/перенос строки или
    отдельными элементами <br>/<li>. Дополнительно разделяем по шаблону
    «Фамилия И.О.» / «Фамилия Имя Отчество», чтобы получить аккуратный список.
    """
    raw = _clean(text)
    if not raw:
        return ""
    parts = re.split(r"\s*[;,\n]\s*|\s{2,}", raw)
    seen: list[str] = []
    for part in parts:
        part = _clean(part).strip(" .;,")
        if part and part not in seen:
            seen.append(part)
    return "; ".join(seen) if len(seen) > 1 else _clean(raw)


def _participants_from_field_map(field_map: dict[str, str]) -> str:
    """Участники из колонки таблицы с подходящим заголовком."""
    for h, v in field_map.items():
        if any(k in h for k in PARTICIPANTS_HEADER_KEYS):
            value = _split_participants(v)
            if value:
                return value
    return ""


def _row_link(row, base_url: str) -> str:
    """Ссылка на карточку дела: предпочтительно из строки результата."""
    a = row.find("a", href=True)
    if not a:
        return ""
    href = (a["href"] or "").strip()
    if not href or href.startswith(("javascript:", "#")):
        return ""
    return urljoin(base_url, href) if base_url else href


def has_cases_table(html: str) -> bool:
    """Есть ли на странице таблица дел (хотя бы с одной строкой дела)."""
    if not html:
        return False
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        if not table.find("th") and not table.find("td"):
            continue
        for row in table.find_all("tr"):
            text = _clean(row.get_text(" ", strip=True))
            if CASE_NUMBER_RE.search(text) or ARTICLE_RE.search(text):
                return True
    return False


def parse_case_html(html: str, url: str = "") -> list[Case]:
    """Разобрать страницу со списком/карточками дел.

    Стратегия устойчива к вёрстке: ищем таблицы результатов, а при их
    отсутствии — повторяющиеся блоки с номером дела. Все поля достаём из
    текста регулярками, поэтому смена классов не ломает парсер полностью.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    cases: list[Case] = []

    # 1) табличный вариант выдачи
    for table in soup.find_all("table"):
        headers = [_clean(th.get_text()) for th in table.find_all("th")]
        if not headers:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue
            cell_texts = [_clean(c.get_text(" ", strip=True)) for c in cells]
            row_text = _clean(" ".join(cell_texts))
            case = _case_from_row(headers, cell_texts, row_text, _row_link(row, url) or url)
            if case:
                cases.append(case)
        if cases:
            return cases

    # 2) блочный вариант (карточки/div-списки)
    for block in soup.find_all(["div", "li", "article"]):
        text = _clean(block.get_text(" ", strip=True))
        if not text or not CASE_NUMBER_RE.search(text):
            continue
        # избегаем дублей от вложенных контейнеров
        if any(text in _clean(c.summary) for c in cases):
            continue
        link = _row_link(block, url) or url
        case = _case_from_row([], [], text, link)
        if case:
            cases.append(case)
    return cases


def _case_from_row(headers: list[str], cells: list[str], row_text: str, url: str) -> Case | None:
    case_number = extract_case_number(row_text)
    article = extract_article(row_text)
    if not case_number and not article:
        return None

    # значения по заголовкам, если они есть
    lower_headers = [h.lower() for h in headers]
    field_map: dict[str, str] = {}
    for idx, h in enumerate(lower_headers):
        if idx < len(cells):
            field_map[h] = cells[idx]
    joined = " ".join(field_map.values()) if field_map else row_text

    # «Дата заседания» и «Время» могут быть отдельными колонками
    separated_date_col = any("дата" in h for h in field_map)
    separated_time_col = any("врем" in h for h in field_map)

    hearing_raw = ""
    for h, v in field_map.items():
        if any(k in h for k in HEARING_HEADER_KEYS):
            hearing_raw = " ".join(p for p in (hearing_raw, v) if p)
    if not hearing_raw:
        hearing_raw = row_text

    date = parse_date(hearing_raw)
    time_raw = ""
    if separated_time_col:
        for h, v in field_map.items():
            if "врем" in h and TIME_RE.search(v or ""):
                time_raw = TIME_RE.search(v).group(0)
                break
    if not time_raw and not (date and (date.hour or date.minute)):
        tm = TIME_RE.search(hearing_raw)
        if tm:
            time_raw = tm.group(0)
    if separated_date_col and date and not (date.hour or date.minute):
        # время было в отдельной колонке и уже сохранено в time_raw
        pass

    court = ""
    district = extract_district(joined)
    for h, v in field_map.items():
        if "суд" in h and "участ" not in h:
            court = v
    if not court:
        court = district or "Мировой судья"

    participants = _participants_from_field_map(field_map)

    return Case(
        case_number=case_number,
        article=article,
        court=court,
        district=district,
        hearing=Hearing(date=date, raw=hearing_raw, time_raw=time_raw),
        url=url,
        summary=_clean(row_text)[:300],
        participants=participants,
    )
