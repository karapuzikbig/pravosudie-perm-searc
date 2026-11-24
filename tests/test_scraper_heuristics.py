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


def test_collect_precinct_urls_drops_portal_and_bare_msudrf():
    s = _scraper()
    page = FakePage(
        "https://sudrf.ru/index.php?id=300&act=go_ms_search",
        [
            "https://sudrf.ru/",
            "http://msudrf.ru/",
            "http://5.perm.msudrf.ru/modules.php?name=sud_delo",
        ],
    )
    urls = asyncio.run(s._collect_precinct_urls(page))
    assert urls == ["http://5.perm.msudrf.ru/modules.php?name=sud_delo"]


def test_clean_ocr_code_keeps_only_cyrillic_lower():
    from src.scraper import _clean_ocr_code

    # латиница-двойники → кириллица, цифры и знаки выкидываем
    assert _clean_ocr_code(" aB-c1 2! ") == "авс"
    assert _clean_ocr_code("mowwaup") == "мошшаир"
    assert _clean_ocr_code("Кот12") == "кот"
    assert _clean_ocr_code(None) == ""
    assert _clean_ocr_code("") == ""


def test_html_has_captcha_by_structure():
    from src.scraper import _html_has_captcha

    assert _html_has_captcha('<form id="kcaptchaForm">…</form>') is True
    assert _html_has_captcha("<input name='captcha-response'>") is True
    assert _html_has_captcha('<img src="/captcha.php?r=1">') is True
    assert _html_has_captcha("<html><body>Список дел</body></html>") is False
    assert _html_has_captcha("captcha.php") is False
    assert _html_has_captcha(None) is False
    assert _html_has_captcha("") is False


def test_vote_codes_picks_most_frequent_then_longest():
    from src.scraper import _vote_codes

    assert _vote_codes(["котш", "котп", "котш"]) == "котш"
    assert _vote_codes(["кот", "ко", "кот"]) == "кот"
    assert _vote_codes(["", None, "ха"]) == "ха"
    assert _vote_codes([]) == ""


def test_prepare_captcha_variants_degrades_on_bad_bytes():
    from src.scraper import _prepare_captcha_variants

    variants = _prepare_captcha_variants(b"not-an-image")
    assert variants == [b"not-an-image"]


def test_vision_solver_no_key_returns_none():
    from src.captcha_ai import solve_captcha_vision

    assert solve_captcha_vision(b"img", api_key="") is None
    assert solve_captcha_vision(b"", api_key="k") is None


class _ScriptedPage:
    """page-заглушка: каждая проверка возвращает очередное значение."""

    def __init__(self, values):
        self._values = list(values)
        self.calls = 0

    async def probe(self):
        self.calls += 1
        return self._values.pop(0) if self._values else self._values_last

    _values_last = False


def test_poll_until_requires_two_consecutive_hits():
    s = _scraper()
    # False, True, False, True, True → успех только на 5-й проверке
    page = _ScriptedPage([False, True, False, True, True])
    ok = asyncio.run(s._poll_until(page, page.probe, timeout_ms=10000))
    assert ok is True
    assert page.calls == 5


def test_poll_until_times_out_when_never_stable():
    s = _scraper()
    # True чередуется с False — два подряд так и не набираются
    page = _ScriptedPage([True, False] * 100)
    ok = asyncio.run(s._poll_until(page, page.probe, timeout_ms=1500))
    assert ok is False


def test_wait_for_case_form_detects_admin_tab():
    s = _scraper()

    class _Loc:
        def __init__(self, n):
            self._n = n

        async def count(self):
            return self._n

    class _Page:
        def locator(self, sel):
            return _Loc(1 if sel in ("#bookmark_type_2", "li#type_2") else 0)

    ok = asyncio.run(s._wait_for_case_form(_Page(), timeout_ms=1000))
    assert ok is True


def test_wait_for_case_form_returns_false_on_empty_skeleton():
    s = _scraper()

    class _Loc:
        async def count(self):
            return 0

    class _Page:
        def locator(self, _sel):
            return _Loc()

    ok = asyncio.run(s._wait_for_case_form(_Page(), timeout_ms=700))
    assert ok is False


def test_captcha_solved_true_when_input_gone():
    s = _scraper()

    class _Loc:
        def __init__(self, n):
            self._n = n

        async def count(self):
            return self._n

    class _Page:
        def locator(self, sel):
            return _Loc(0 if sel == 'input[name="captcha-response"]' else 0)

    assert asyncio.run(s._captcha_solved(_Page())) is True


def test_captcha_solved_true_when_case_form_present():
    s = _scraper()

    class _Loc:
        def __init__(self, n):
            self._n = n

        async def count(self):
            return self._n

    class _Page:
        def locator(self, sel):
            if sel == 'input[name="captcha-response"]':
                return _Loc(1)
            return _Loc(1 if sel in ("#bookmark_type_2", "li#type_2") else 0)

    assert asyncio.run(s._captcha_solved(_Page())) is True


def test_captcha_solved_false_when_input_still_there_and_no_form():
    s = _scraper()

    class _Loc:
        def __init__(self, n):
            self._n = n

        async def count(self):
            return self._n

    class _Page:
        def locator(self, sel):
            return _Loc(1 if sel == 'input[name="captcha-response"]' else 0)

    assert asyncio.run(s._captcha_solved(_Page())) is False



def test_captcha_field_present_true_when_input_there():
    s = _scraper()

    class _Loc:
        def __init__(self, n):
            self._n = n

        async def count(self):
            return self._n

    class _Page:
        def locator(self, sel):
            return _Loc(1 if sel == 'input[name="captcha-response"]' else 0)

        async def content(self):
            return '<input name="captcha-response">'

    assert asyncio.run(s._captcha_field_present(_Page())) is True


def test_captcha_field_present_false_when_skeleton_without_captcha():
    s = _scraper()

    class _Loc:
        async def count(self):
            return 0

    class _Page:
        def locator(self, sel):
            return _Loc()

        async def content(self):
            return "<html><body><div id='app'></div></body></html>"

    assert asyncio.run(s._captcha_field_present(_Page())) is False


def test_ddddocr_disabled_by_default():
    from src.config import ScraperConfig

    assert ScraperConfig().captcha_use_ddddocr is False


def test_open_case_search_no_form_branch_does_not_await_sync_pause(monkeypatch):
    # регрессия: _pause синхронная, после капчи без формы её звали через await,
    # из-за чего участок падал с "object NoneType can't be used in 'await' expression"
    s = _scraper()
    monkeypatch.setattr(s, "cfg", type(s.cfg)(delay_min_between_requests=0, delay_max_between_requests=0))

    async def _not_blocked(_page):
        return False

    async def _no_captcha(_page):
        return False

    async def _no_form(_page):
        return False

    async def _no_links(_page, _texts):
        return False

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(s, "_is_blocked", _not_blocked)
    monkeypatch.setattr(s, "_has_captcha", _no_captcha)
    monkeypatch.setattr(s, "_page_has_form", _no_form)
    monkeypatch.setattr(s, "_click_link_texts", _no_links)
    monkeypatch.setattr(s, "_dump", _noop)
    monkeypatch.setattr(s, "_open_site_with_retry", lambda *_a, **_k: _true())

    class _Page:
        async def goto(self, *_a, **_k):
            return None

        async def content(self):
            return "<html></html>"

    result = asyncio.run(s._open_case_search_on_site(_Page(), "http://1.perm.msudrf.ru", False))
    assert result is False


async def _true():
    return True


def test_open_case_search_returns_true_after_captcha_without_form(monkeypatch):
    # после принятой капчи страница — JS-скелет без <form>; участок нельзя
    # сразу помечать «не открылся», отрисовку ждёт _wait_for_case_form
    s = _scraper()
    monkeypatch.setattr(s, "cfg", type(s.cfg)(delay_min_between_requests=0, delay_max_between_requests=0))

    async def _not_blocked(_page):
        return False

    async def _captcha(_page):
        return True

    async def _pass(_page, _dump):
        return None

    async def _no_form(_page):
        return False

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(s, "_is_blocked", _not_blocked)
    monkeypatch.setattr(s, "_has_captcha", _captcha)
    monkeypatch.setattr(s, "_pass_captcha", _pass)
    monkeypatch.setattr(s, "_page_has_form", _no_form)
    monkeypatch.setattr(s, "_dump", _noop)
    monkeypatch.setattr(s, "_open_site_with_retry", lambda *_a, **_k: _true())

    class _Page:
        async def goto(self, *_a, **_k):
            return None

        async def content(self):
            return "<html></html>"

    result = asyncio.run(s._open_case_search_on_site(_Page(), "http://1.perm.msudrf.ru", False))
    assert result is True
