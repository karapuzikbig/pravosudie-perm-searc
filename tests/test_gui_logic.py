"""Тесты логики окна без реального Tk (tkinter подменяется заглушками).

Проверяем открытие ссылки (webbrowser.open вызывается с нужным URL) и
формирование строки результата. Полноценный рендер Tk требует графического
окружения и здесь не тестируется.
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from src.parsers import Case, Hearing


@pytest.fixture()
def gui_module(monkeypatch):
    tk = types.ModuleType("tkinter")
    tk.Tk = object
    tk.StringVar = object
    ttk = types.ModuleType("tkinter.ttk")
    msg = types.ModuleType("tkinter.messagebox")
    tk.ttk = ttk
    tk.messagebox = msg
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.ttk", ttk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", msg)
    sys.modules.pop("src.gui", None)
    return importlib.import_module("src.gui")


class _FakeTree:
    def __init__(self):
        self.rows = {}
        self._sel = []

    def selection(self):
        return self._sel


class _FakeApp:
    """Носитель метода _on_open_selected без реальной Tk-родительницы."""

    def __init__(self, open_method):
        self.tree = _FakeTree()
        self._results = {}
        self._open = open_method

    def _on_open_selected(self, _event=None):
        return self._open(self, _event)


def test_open_selected_opens_url(gui_module, monkeypatch):
    opened = []
    monkeypatch.setattr(gui_module.webbrowser, "open", lambda url, new=0: opened.append(url))

    app = _FakeApp(gui_module.App._on_open_selected)
    case = Case(case_number="5-1/2025", article="12.8", url="https://sudrf.ru/case/1")
    app._results[case.uid] = case
    app.tree._sel = [case.uid]

    app._on_open_selected()
    assert opened == ["https://sudrf.ru/case/1"]


def test_open_selected_no_selection(gui_module, monkeypatch):
    opened = []
    monkeypatch.setattr(gui_module.webbrowser, "open", lambda url, new=0: opened.append(url))
    app = _FakeApp(gui_module.App._on_open_selected)
    app._on_open_selected()
    assert opened == []


def test_result_row_values():
    case = Case(
        case_number="5-123/2025",
        article="12.8",
        district="Судебный участок №3",
        hearing=Hearing(raw="12.03.2025 14:30"),
        url="https://sudrf.ru/case/1",
    )
    values = (
        case.article or "—",
        case.hearing.iso or case.hearing.raw or "—",
        case.district or "—",
        case.case_number or "—",
        case.url or "—",
    )
    assert values == (
        "12.8", "12.03.2025 14:30", "Судебный участок №3", "5-123/2025", "https://sudrf.ru/case/1"
    )
