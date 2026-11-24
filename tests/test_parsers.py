"""Юнит-тесты парсеров (без сети, на фикстурах HTML)."""
from __future__ import annotations

from datetime import datetime

from src.parsers import (
    Case,
    Hearing,
    extract_article,
    extract_case_number,
    extract_district,
    parse_case_html,
    parse_date,
)

TABLE_HTML = """
<html><body>
<table>
  <tr><th>Номер дела</th><th>Статья</th><th>Судебный участок</th><th>Дата заседания</th></tr>
  <tr>
    <td><a href="/modules.php?id=5-123/2025">5-123/2025</a></td>
    <td>12.8 ч.1 КоАП РФ</td>
    <td>Судебный участок №3</td>
    <td>12.03.2025 14:30</td>
  </tr>
  <tr>
    <td>5-200/2025</td>
    <td>20.20 ч.1 КоАП РФ</td>
    <td>Судебный участок №7</td>
    <td>05.04.2025 09:00</td>
  </tr>
</table>
</body></html>
"""

BLOCK_HTML = """
<html><body>
<div class="case">
  <a href="https://sudrf.ru/case/1">Дело 5-77/2025</a>
  <span>Ст. 6.9 КоАП РФ</span>
  <span>Судебный участок №12</span>
  <span>Заседание 20.05.2025 11:15</span>
</div>
</body></html>
"""


def test_parse_date_with_time():
    assert parse_date("12.03.2025 14:30") == datetime(2025, 3, 12, 14, 30)


def test_parse_date_invalid():
    assert parse_date("нет даты") is None
    assert parse_date("32.13.2025") is None


def test_extract_fields():
    text = "5-123/2025 ст. 12.26 ч.1 КоАП РФ, судебный участок №5"
    assert extract_case_number(text) == "5-123/2025"
    assert extract_article(text) == "12.26"
    assert extract_district(text) == "Судебный участок №5"


def test_parse_table_html_with_links():
    cases = parse_case_html(TABLE_HTML, url="https://sudrf.ru/search")
    assert len(cases) == 2
    first = cases[0]
    assert first.case_number == "5-123/2025"
    assert first.article == "12.8"
    assert first.district == "Судебный участок №3"
    assert first.hearing.iso == "2025-03-12 14:30"
    # относительная ссылка из строки разрешена в абсолютную
    assert first.url == "https://sudrf.ru/modules.php?id=5-123/2025"


def test_parse_block_html_absolute_link():
    cases = parse_case_html(BLOCK_HTML)
    assert len(cases) == 1
    c = cases[0]
    assert c.case_number == "5-77/2025"
    assert c.article == "6.9"
    assert c.hearing.iso == "2025-05-20 11:15"
    assert c.url == "https://sudrf.ru/case/1"


def test_matches_articles_prefix():
    case = Case(case_number="1", article="12.8")
    assert case.matches_articles(["12.8"])
    assert case.matches_articles(["12"])
    assert not case.matches_articles(["20.20"])
    c26 = Case(case_number="2", article="12.26")
    assert not c26.matches_articles(["12.8"])
    assert c26.matches_articles(["12.26"])


def test_uid_stable():
    a = Case(case_number="5-1/2025", article="12.8", hearing=Hearing(raw="12.03.2025"))
    b = Case(case_number="5-1/2025", article="12.8", hearing=Hearing(raw="12.03.2025"))
    assert a.uid == b.uid


PARTICIPANTS_TABLE_HTML = """
<html><body>
<table>
  <tr><th>Номер дела</th><th>Дата заседания</th><th>Время</th><th>Участники дела</th></tr>
  <tr>
    <td><a href="/modules.php?id=5-321/2025">5-321/2025</a></td>
    <td>18.06.2025</td>
    <td>10:15</td>
    <td>Иванов И.И., Петров П.П.</td>
  </tr>
</table>
</body></html>
"""


def test_parse_participants_and_separate_date_time():
    cases = parse_case_html(PARTICIPANTS_TABLE_HTML, url="https://sudrf.ru/search")
    assert len(cases) == 1
    c = cases[0]
    assert c.case_number == "5-321/2025"
    assert c.hearing.date_str == "18.06.2025"
    assert c.hearing.time_str == "10:15"
    assert "Иванов И.И" in c.participants
    assert "Петров П.П" in c.participants


def test_has_cases_table():
    from src.parsers import has_cases_table

    assert has_cases_table(PARTICIPANTS_TABLE_HTML) is True
    assert has_cases_table(TABLE_HTML) is True
    assert has_cases_table("<html><body>Ничего не найдено</body></html>") is False
    assert has_cases_table("") is False


def test_dedup_key_distinguishes_hearings():
    a = Case(case_number="5-1/2025", article="12.8", hearing=Hearing(raw="12.03.2025 14:30"))
    b = Case(case_number="5-1/2025", article="12.8", hearing=Hearing(raw="20.03.2025 10:00"))
    assert a.dedup_key != b.dedup_key
    same = Case(case_number="5-1/2025", article="12.8", hearing=Hearing(raw="12.03.2025 14:30"))
    assert a.dedup_key == same.dedup_key
