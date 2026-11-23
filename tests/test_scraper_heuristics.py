"""Тесты эвристик скрейпера без браузера (фейковые локаторы Playwright)."""
from __future__ import annotations

import asyncio

from src.config import load_settings
from src.scraper import Scraper


class FakeOption:
    def __init__(self, text, value):
        self._text = text
        self._value = value

    async def inner_text(self):
        return self._text

    async def get_attribute(self, name):
        return self._value if name == "value" else None


class FakeList:
    def __init__(self, items):
        self._items = items

    async def all(self):
        return self._items

    async def count(self):
        return len(self._items)


class FakeSelect:
    def __init__(self, name, options):
        self.name = name
        self.options = [FakeOption(t, v) for t, v in options]
        self.selected = None

    async def get_attribute(self, name):
        return self.name if name == "name" else None

    def locator(self, _sel):
        return FakeList(self.options)

    async def select_option(self, value=None, label=None):
        for o in self.options:
            if value is not None and o._value == value:
                self.selected = o._text
                return
            if label is not None and o._text == label:
                self.selected = o._text
                return
        raise ValueError("no option")


def _scraper():
    settings = load_settings()
    return Scraper(settings)


def test_select_key_detects_regions():
    s = _scraper()
    sel = FakeSelect("region", [("Пермский край", "59"), ("Свердловская область", "66")])
    assert asyncio.run(s._select_key(sel)) == "region"


def test_select_option_by_hint_picks_perm_region():
    s = _scraper()
    sel = FakeSelect("subject", [("Свердловская область", "66"), ("Пермский край", "59")])
    assert asyncio.run(s._select_option_by_hint(sel, ("пермск",)))
    assert sel.selected == "Пермский край"


def test_select_key_detects_court():
    s = _scraper()
    sel = FakeSelect("x", [("Мировой судья", "1"), ("Районный суд", "2")])
    assert asyncio.run(s._select_key(sel)) == "court"


def test_select_option_by_text_exact():
    s = _scraper()
    sel = FakeSelect("city", [("Пермь", "p"), ("Березники", "b")])
    assert asyncio.run(s._select_option_by_text(sel, "Пермь"))
    assert sel.selected == "Пермь"


class FakeAnchor:
    def __init__(self, href):
        self._href = href

    async def get_attribute(self, name):
        return self._href if name == "href" else None


class FakePage:
    def __init__(self, url, hrefs):
        self.url = url
        self._hrefs = hrefs

    def locator(self, _sel):
        return FakeList([FakeAnchor(h) for h in self._hrefs])


def test_collect_precinct_urls_filters_and_absolutizes():
    s = _scraper()
    page = FakePage(
        "https://sudrf.ru/index.php?id=300&act=go_ms_search",
        [
            "http://59.perm.msudrf.ru/",
            "/index.php?id=300&page=2",
            "http://1.perm.msudrf.ru/",
            "javascript:void(0)",
            "#ms",
            "http://59.perm.msudrf.ru/",
            "http://counter.sudrf.ru/cnt.php",
        ],
    )
    urls = asyncio.run(s._collect_precinct_urls(page))
    assert urls == ["http://59.perm.msudrf.ru/", "http://1.perm.msudrf.ru/"]


class FakeContentPage:
    def __init__(self, html):
        self._html = html

    async def content(self):
        return self._html


def test_is_blocked_detects_403():
    s = _scraper()
    blocked = FakeContentPage("<html><body>403 Доступ запрещен</body></html>")
    ok = FakeContentPage("<html><body>Список дел</body></html>")
    assert asyncio.run(s._is_blocked(blocked)) is True
    assert asyncio.run(s._is_blocked(ok)) is False
