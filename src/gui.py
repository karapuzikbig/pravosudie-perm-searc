"""Окно программы на tkinter: ввод статей и список найденных дел со ссылками.

Клик по строке (или двойной клик) открывает карточку дела в системном
браузере. Срейпинг выполняется в фоновом потоке, чтобы окно не подвисало;
результаты передаются в главный поток Tk через очередь.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .config import Settings, parse_articles
from .parsers import Case
from .scraper import CaptchaDetected, Scraper


class App(tk.Tk):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.scraper = Scraper(settings)
        self._results: dict[str, Case] = {}
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._worker: threading.Thread | None = None

        self.title("Поиск дел — мировые судьи г. Перми (ГАС «Правосудие»)")
        self.geometry("980x560")
        self.minsize(720, 400)

        self._build_widgets()
        self.after(150, self._drain_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ----------------------------------------------------------------- UI
    def _build_widgets(self) -> None:
        top = ttk.Frame(self, padding=(12, 10))
        top.pack(fill="x")

        ttk.Label(top, text="Статьи КоАП (через запятую):").pack(side="left")

        self.articles_var = tk.StringVar(
            value=", ".join(self.settings.default_articles)
        )
        entry = ttk.Entry(top, textvariable=self.articles_var, width=42)
        entry.pack(side="left", padx=8, ipady=3)
        entry.bind("<Return>", lambda _e: self._on_search())

        self.search_btn = ttk.Button(top, text="Найти", command=self._on_search)
        self.search_btn.pack(side="left")

        ttk.Label(
            top,
            text=f"{self.settings.region}, {self.settings.city} · {self.settings.court_type}",
            foreground="#666",
        ).pack(side="left", padx=12)

        # Таблица результатов
        table_frame = ttk.Frame(self, padding=(12, 0))
        table_frame.pack(fill="both", expand=True)

        columns = ("article", "hearing", "district", "case", "url")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "article": ("Статья", 80),
            "hearing": ("Заседание", 150),
            "district": ("Участок", 160),
            "case": ("Дело", 140),
            "url": ("Ссылка", 420),
        }
        for col, (text, width) in headings.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w", stretch=(col == "url"))

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_open_selected)
        self.tree.bind("<Return>", self._on_open_selected)

        # Нижняя панель
        bottom = ttk.Frame(self, padding=(12, 8))
        bottom.pack(fill="x")

        self.status_var = tk.StringVar(value="Готово. Впишите статьи и нажмите «Найти».")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")

        self.open_btn = ttk.Button(
            bottom, text="Открыть в браузере", command=self._on_open_selected, state="disabled"
        )
        self.open_btn.pack(side="right")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.open_btn.configure(state="normal"))

        hint = ttk.Label(
            self,
            text="Двойной клик по строке открывает карточку дела в браузере.",
            foreground="#888",
            padding=(12, 0, 12, 8),
        )
        hint.pack(fill="x")

    # -------------------------------------------------------------- поиск
    def _on_search(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        articles = parse_articles(self.articles_var.get(), fallback=self.settings.default_articles)
        if not articles:
            messagebox.showwarning("Статьи не заданы", "Впишите хотя бы одну статью КоАП, например: 12.8, 20.20")
            return

        self._results.clear()
        self.tree.delete(*self.tree.get_children())
        self.search_btn.configure(state="disabled")
        self.status_var.set(f"Идёт поиск по статьям: {', '.join(articles)}…")

        self._worker = threading.Thread(
            target=self._search_worker, args=(articles,), daemon=True
        )
        self._worker.start()

    def _search_worker(self, articles: list[str]) -> None:
        try:
            cases = asyncio.run(self.scraper.fetch_cases(articles))
            self._queue.put(("done", cases))
        except CaptchaDetected as exc:
            self._queue.put(("error", f"Сайт требует капчу. Проверьте вручную в браузере. ({exc})"))
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("error", f"Ошибка поиска: {exc}"))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "done":
                    self._show_results(payload)  # type: ignore[arg-type]
                elif kind == "error":
                    self._show_error(str(payload))
        except queue.Empty:
            pass
        self.after(150, self._drain_queue)

    def _show_results(self, cases: list[Case]) -> None:
        self.search_btn.configure(state="normal")
        for case in cases:
            self._results[case.uid] = case
            self.tree.insert(
                "",
                "end",
                iid=case.uid,
                values=(
                    case.article or "—",
                    case.hearing.iso or case.hearing.raw or "—",
                    case.district or "—",
                    case.case_number or "—",
                    case.url or "—",
                ),
            )
        n = len(cases)
        self.status_var.set(
            f"Найдено дел: {n}." if n else "Ничего не найдено. Попробуйте другие статьи или позже."
        )

    def _show_error(self, message: str) -> None:
        self.search_btn.configure(state="normal")
        self.status_var.set("Ошибка поиска.")
        messagebox.showerror("Ошибка", message)

    # ------------------------------------------------------------ открытие
    def _on_open_selected(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        case = self._results.get(sel[0])
        if not case:
            return
        url = case.url
        if not url:
            messagebox.showinfo(
                "Нет ссылки",
                "Для этого дела не удалось получить прямую ссылку.\n"
                "Откройте портал ГАС «Правосудие» и найдите дело по номеру.",
            )
            return
        webbrowser.open(url, new=2)

    def _on_close(self) -> None:
        self.destroy()


def run(settings: Settings | None = None) -> None:
    from .config import load_settings

    app = App(settings or load_settings())
    app.mainloop()
