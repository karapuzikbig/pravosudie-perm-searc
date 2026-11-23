"""Юнит-тесты хранилища: антидубли и смена даты заседания."""
from __future__ import annotations

from datetime import datetime

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


def test_settings_roundtrip(tmp_path):
    st = Storage(tmp_path / "test.db")
    assert st.get_setting("articles", ["x"]) == ["x"]
    st.set_setting("articles", ["12.8", "20.20"])
    assert st.get_setting("articles") == ["12.8", "20.20"]
    assert st.get_setting("muted", False) is False
