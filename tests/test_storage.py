"""Юнит-тесты хранилища и разбора списка статей."""
from __future__ import annotations

from datetime import datetime

from src.config import parse_articles
from src.parsers import Case, Hearing
from src.storage import Storage


def _case(number="5-1/2025", iso=None, raw="12.03.2025 14:30"):
    dt = datetime.strptime(iso, "%Y-%m-%d %H:%M") if iso else None
    return Case(
        case_number=number,
        article="12.8",
        district="Судебный участок №3",
        hearing=Hearing(date=dt, raw=raw),
    )


def test_new_changed_same(tmp_path):
    st = Storage(tmp_path / "test.db")
    c = _case(iso="2025-03-12 14:30")

    assert st.upsert_case(c) == "new"
    assert st.count() == 1
    assert st.upsert_case(c) == "same"

    moved = _case(iso="2025-03-20 10:00", raw="20.03.2025 10:00")
    assert st.upsert_case(moved) == "changed"
    assert st.count() == 1


def test_parse_articles_variants():
    assert parse_articles("12.8, 20.20") == ["12.8", "20.20"]
    assert parse_articles("12.8 20.20; 6.9") == ["12.8", "20.20", "6.9"]
    assert parse_articles("12.8,\n20.20 ,") == ["12.8", "20.20"]
    assert parse_articles("12.8, 12.8") == ["12.8"]
    assert parse_articles("", fallback=["6.9"]) == ["6.9"]
