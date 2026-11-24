"""Тесты экспорта в Excel с дозаписью и антидублями."""
from __future__ import annotations

from datetime import datetime

from openpyxl import load_workbook

from src.excel_export import HEADERS, append_cases
from src.parsers import Case, Hearing


def _case(number="5-1/2025", date="12.03.2025", time_="14:30", participants="Иванов И.И."):
    dt = datetime.strptime(f"{date} {time_}", "%d.%m.%Y %H:%M")
    return Case(
        case_number=number,
        article="12.8",
        hearing=Hearing(date=dt, raw=f"{date} {time_}"),
        participants=participants,
    )


def test_creates_file_with_header(tmp_path):
    path = tmp_path / "cases.xlsx"
    added = append_cases(path, [_case()])
    assert added == 1
    ws = load_workbook(path)["Дела"]
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0] == HEADERS
    assert rows[1] == ("5-1/2025", "12.03.2025", "14:30", "Иванов И.И.")


def test_skips_duplicates_and_appends_new(tmp_path):
    path = tmp_path / "cases.xlsx"
    append_cases(path, [_case()])
    assert append_cases(path, [_case()]) == 0
    assert append_cases(path, [_case(date="20.03.2025", time_="10:00")]) == 1
    rows = list(load_workbook(path)["Дела"].iter_rows(values_only=True))
    assert len(rows) == 3
    assert rows[2][1] == "20.03.2025"


def test_empty_input_no_file(tmp_path):
    path = tmp_path / "cases.xlsx"
    assert append_cases(path, []) == 0
    assert not path.exists()
