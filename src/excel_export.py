"""Экспорт найденных дел в Excel (.xlsx) с дозаписью новых строк.

Приложение дописывает только новые дела (по номеру + дате + времени
заседания) в конец существующего файла, не перезаписывая прежние сроки.
Это позволяет прогонять парсер по сотне страниц по частям и не терять
уже собранные данные между запусками.

Файл открывается заново на каждую запись, поэтому результат выглядит
корректно даже если пользователь параллельно смотрит его в Excel/проводнике.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .parsers import Case

log = logging.getLogger(__name__)

SHEET_TITLE = "Дела"
HEADERS = ("Номер дела", "Дата", "Время", "Участники дела")
COLUMN_WIDTHS = (20, 14, 10, 70)
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")


def _header_row(ws) -> int | None:
    """Найти строку заголовков по тексту первой колонки («Номер дела»)."""
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 5)):
        for cell in row:
            if str(cell.value or "").strip().lower() == HEADERS[0].lower():
                return cell.row
    return None


def _existing_keys(ws, header_row: int | None) -> set[str]:
    """Собрать ключи антидублей из уже записанных строк."""
    keys: set[str] = set()
    start = (header_row or 0) + 1
    for row in ws.iter_rows(min_row=start, values_only=True):
        number = str(row[0] or "").strip() if len(row) > 0 else ""
        if not number:
            continue
        date = str(row[1] or "").strip() if len(row) > 1 else ""
        time_ = str(row[2] or "").strip() if len(row) > 2 else ""
        participants = str(row[3] or "").strip() if len(row) > 3 else ""
        keys.add(_row_key(number, date, time_, participants))
    return keys


def _row_key(number: str, date: str, time_: str, participants: str) -> str:
    """Ключ строки — тот же алгоритм, что Case.dedup_key."""
    parts = "|".join(
        [
            (number or "").strip(),
            (date or "").strip(),
            (time_ or "").strip(),
            (participants or "").strip().lower(),
        ]
    )
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()[:16]


def _write_header(ws) -> None:
    ws.append(list(HEADERS))
    for col, width in enumerate(COLUMN_WIDTHS, start=1):
        cell = ws.cell(row=1, column=col)
        cell.font = Font(bold=True)
        cell.fill = HEADER_FILL
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.freeze_panes = "A2"


def append_cases(path: str | Path, cases: list[Case]) -> int:
    """Дописать новые дела в xlsx. Возвращает число добавленных строк.

    Дубли (то же дело с той же датой/временем заседания) пропускаются.
    Если файла ещё нет — он создаётся вместе с шапкой таблицы.
    """
    path = Path(path)
    if not cases:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        workbook = load_workbook(path)
        ws = workbook[SHEET_TITLE] if SHEET_TITLE in workbook.sheetnames else workbook.active
        header_row = _header_row(ws)
        if header_row is None:
            _write_header(ws)
            header_row = 1
        seen = _existing_keys(ws, header_row)
    else:
        workbook = Workbook()
        ws = workbook.active
        ws.title = SHEET_TITLE
        _write_header(ws)
        seen = set()

    added = 0
    for case in cases:
        key = case.dedup_key
        if key in seen:
            continue
        seen.add(key)
        ws.append(
            [
                case.case_number or "",
                case.hearing.date_str or "",
                case.hearing.time_str or "",
                case.participants or "",
            ]
        )
        added += 1

    if added or not path.exists():
        workbook.save(path)
        log.info("Excel: добавлено %d новых строк в %s", added, path)
    return added
