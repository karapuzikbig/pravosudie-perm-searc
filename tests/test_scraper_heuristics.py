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


def test_clean_ocr_code_keeps_cyrillic_only():
    from src.scraper import _clean_ocr_code

    assert _clean_ocr_code(" ПР-ив ет 1! ") == "ПРивет"
    assert _clean_ocr_code("roze") == ""
    assert _clean_ocr_code("пAвB") == "пв"
    assert _clean_ocr_code(None) == ""
    assert _clean_ocr_code("") == ""


def test_apply_input_case_defaults_to_lower():
    s = _scraper()
    assert s.cfg.captcha_input_case == "lower"
    assert s._apply_input_case("АБВГД") == "абвгд"
    s.cfg.captcha_input_case = "upper"
    assert s._apply_input_case("абвгд") == "АБВГД"
    s.cfg.captcha_input_case = "asis"
    assert s._apply_input_case("АбВгД") == "АбВгД"


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

    assert _vote_codes(["абвг", "абвд", "абвг"]) == "абвг"
    assert _vote_codes(["абв", "аб", "абв"]) == "абв"
    assert _vote_codes(["", None, "ху"]) == "ху"
    assert _vote_codes(["abcd", "abcf"]) == ""
    assert _vote_codes([]) == ""


def test_prepare_captcha_variants_degrades_on_bad_bytes():
    from src.scraper import _prepare_captcha_variants

    variants = _prepare_captcha_variants(b"not-an-image")
    assert variants == [b"not-an-image"]


def test_clean_captcha_for_vision_degrades_on_bad_bytes():
    from src.scraper import _clean_captcha_for_vision

    assert _clean_captcha_for_vision(b"not-an-image") == b"not-an-image"


def test_vision_solver_no_key_returns_none():
    from src.captcha_ai import solve_captcha_vision

    assert solve_captcha_vision(b"img", api_key="") is None
    assert solve_captcha_vision(b"", api_key="k") is None


def test_vision_ocr_returns_single_code():
    s = _scraper()
    import src.captcha_ai as ai_mod

    calls = iter(["абвгд", "абвгде"])
    orig = ai_mod.solve_captcha_vision
    ai_mod.solve_captcha_vision = lambda _img, **_kw: next(calls, "")
    try:
        code = s._vision_ocr(b"not-an-image")
    finally:
        ai_mod.solve_captcha_vision = orig
    assert isinstance(code, str)
    assert code != ""


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





def test_html_is_500_detects_server_error():
    from src.scraper import _html_is_500

    assert _html_is_500("<h1>500 Internal Server Error</h1>") is True
    assert _html_is_500("<title>Ошибка 500</title><body>Произошла ошибка</body>") is True
    assert _html_is_500("<html><body>Список дел 5-1/2025</body></html>") is False
    assert _html_is_500("") is False
    assert _html_is_500(None) is False


def test_recover_from_500_disabled():
    s = _scraper()
    s.cfg.retry_on_500 = False

    class _P:
        async def content(self):
            return "500 Internal Server Error"

    # при отключённой опции считаем, что всё в порядке (нечего чинить)
    assert asyncio.run(s._recover_from_500(_P(), "test")) is True


def test_recover_from_500_reloads_then_succeeds():
    s = _scraper()
    s.cfg.retry_on_500 = True
    s.cfg.max_500_reloads = 3
    s._pause = lambda: None

    class _P:
        url = "https://example.msudrf.ru/"

        def __init__(self):
            self.state = "500"
            self.reloads = 0

        async def content(self):
            return "500 Internal Server Error" if self.state == "500" else "<table>ok</table>"

        async def reload(self, wait_until="domcontentloaded"):
            self.reloads += 1
            self.state = "ok"

    p = _P()
    assert asyncio.run(s._recover_from_500(p, "test")) is True
    assert p.reloads == 1


def test_vision_empty_content_returns_none(monkeypatch):
    import json as _json

    from src import captcha_ai

    class _Resp:
        def __init__(self, body):
            self._b = _json.dumps(body).encode()

        def read(self):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    body = {
        "choices": [
            {"message": {"content": "", "reasoning_content": "..."}, "finish_reason": "length"}
        ]
    }
    monkeypatch.setattr(captcha_ai.urllib.request, "urlopen", lambda *a, **k: _Resp(body))
    assert captcha_ai.solve_captcha_vision(b"img", api_key="k") is None

    ok = {"choices": [{"message": {"content": "роЗе"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(captcha_ai.urllib.request, "urlopen", lambda *a, **k: _Resp(ok))
    assert captcha_ai.solve_captcha_vision(b"img", api_key="k") == "роЗе"

    latin = {"choices": [{"message": {"content": "roZe"}, "finish_reason": "stop"}]}
    monkeypatch.setattr(captcha_ai.urllib.request, "urlopen", lambda *a, **k: _Resp(latin))
    assert captcha_ai.solve_captcha_vision(b"img", api_key="k") is None
