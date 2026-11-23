"""SQLite-хранилище: история дел, антидубли, подписки, настройки статей."""
from __future__ import annotations

import json
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
    last_seen     TEXT NOT NULL,
    notified_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cases_hearing ON cases(hearing_iso);
CREATE INDEX IF NOT EXISTS idx_cases_article ON cases(article);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
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

    # ------------------------------------------------------------------ дела
    def upsert_case(self, case: Case, now: datetime | None = None) -> str:
        """Сохранить/обновить дело.

        Возвращает статус:
          "new"      — дело раньше не встречалось;
          "changed"  — изменилась дата/время заседания (перенос);
          "same"     — без изменений.
        """
        now_iso = (now or datetime.now()).isoformat(timespec="seconds")
        with self._conn() as conn:
            row = conn.execute(
                "SELECT hearing_iso, hearing_raw, notified_at FROM cases WHERE uid = ?",
                (case.uid,),
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
            new_iso = case.hearing.iso
            conn.execute(
                """UPDATE cases SET case_number=?, article=?, court=?, district=?,
                       hearing_raw=?, hearing_iso=?, url=?, summary=?, last_seen=?
                   WHERE uid=?""",
                (
                    case.case_number, case.article, case.court, case.district,
                    case.hearing.raw, new_iso, case.url, case.summary, now_iso, case.uid,
                ),
            )
            if new_iso and old_iso != new_iso:
                return "changed"
            return "same"

    def mark_notified(self, uid: str, now: datetime | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE cases SET notified_at = ? WHERE uid = ?",
                ((now or datetime.now()).isoformat(timespec="seconds"), uid),
            )

    def all_cases(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM cases ORDER BY hearing_iso IS NULL, hearing_iso ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def upcoming_cases(self, limit: int = 20) -> list[dict]:
        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM cases WHERE hearing_iso IS NOT NULL AND hearing_iso >= ?
                   ORDER BY hearing_iso ASC LIMIT ?""",
                (now_iso, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]

    # --------------------------------------------------------------- настройки
    def get_setting(self, key: str, default=None):
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
