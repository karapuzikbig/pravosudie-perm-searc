"""SQLite-хранилище найденных дел (история поиска для окна программы)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .parsers import Case

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    uid           TEXT PRIMARY KEY,
    case_number   TEXT,
    article       TEXT,
    court         TEXT,
    district      TEXT,
    hearing_raw   TEXT,
    hearing_iso   TEXT,
    url           TEXT,
    summary       TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_hearing ON cases(hearing_iso);
CREATE INDEX IF NOT EXISTS idx_cases_article ON cases(article);
"""


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_case(self, case: Case, now: datetime | None = None) -> str:
        """Сохранить/обновить дело. Возвращает 'new' | 'changed' | 'same'."""
        now_iso = (now or datetime.now()).isoformat(timespec="seconds")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT hearing_iso FROM cases WHERE uid = ?", (case.uid,)
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO cases (uid, case_number, article, court, district,
                        hearing_raw, hearing_iso, url, summary, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        case.uid, case.case_number, case.article, case.court, case.district,
                        case.hearing.raw, case.hearing.iso, case.url, case.summary,
                        now_iso, now_iso,
                    ),
                )
                return "new"

            old_iso = row["hearing_iso"]
            conn.execute(
                """UPDATE cases SET case_number=?, article=?, court=?, district=?,
                       hearing_raw=?, hearing_iso=?, url=?, summary=?, last_seen=?
                   WHERE uid=?""",
                (
                    case.case_number, case.article, case.court, case.district,
                    case.hearing.raw, case.hearing.iso, case.url, case.summary, now_iso, case.uid,
                ),
            )
            if case.hearing.iso and old_iso != case.hearing.iso:
                return "changed"
            return "same"

    def all_cases(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cases ORDER BY hearing_iso IS NULL, hearing_iso ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
