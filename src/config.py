"""Загрузка конфигурации приложения (config.yaml).

Статьи КоАП задаются пользователем в окне программы, поэтому в конфиге
они необязательны (служат значением по умолчанию для поля ввода).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class ScraperConfig:
    headless: bool = True
    timeout_ms: int = 30000
    delay_min_between_requests: float = 1.5
    delay_max_between_requests: float = 3.5
    max_pages: int = 20
    screenshot_on_error: bool = True


@dataclass
class Settings:
    region: str
    city: str
    court_type: str
    default_articles: list[str] = field(default_factory=list)
    districts: list[str] = field(default_factory=list)
    lookback_days: int = 7
    scraper: ScraperConfig = field(default_factory=ScraperConfig)

    # из .env / окружения
    db_path: str = "data/pravosudie.db"
    log_level: str = "INFO"

    def effective_articles(self, raw: str | None = None) -> list[str]:
        """Разобрать строку статей из поля ввода ('12.8, 20.20 6.9')."""
        if raw is None:
            return list(self.default_articles)
        return parse_articles(raw, fallback=self.default_articles)


def parse_articles(raw: str, fallback: list[str] | None = None) -> list[str]:
    """Разобрать статьи, перечисленные через запятую/пробел/перенос строки."""
    parts = [p for chunk in (raw or "").replace(";", ",").split(",") for p in chunk.split()]
    result = []
    for p in parts:
        p = p.strip().strip(".,")
        if p and p not in result:
            result.append(p)
    return result or list(fallback or [])


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_settings(config_path: str | Path | None = None, env_path: str | Path | None = None) -> Settings:
    load_dotenv(env_path or BASE_DIR / ".env")

    path = Path(config_path) if config_path else BASE_DIR / "config.yaml"
    raw: dict[str, Any] = {}
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    scraper_raw = raw.get("scraper") or {}
    scraper = ScraperConfig(
        headless=bool(scraper_raw.get("headless", True)),
        timeout_ms=int(scraper_raw.get("timeout_ms", 30000)),
        delay_min_between_requests=float(scraper_raw.get("delay_min_between_requests", 1.5)),
        delay_max_between_requests=float(scraper_raw.get("delay_max_between_requests", 3.5)),
        max_pages=int(scraper_raw.get("max_pages", 20)),
        screenshot_on_error=bool(scraper_raw.get("screenshot_on_error", True)),
    )

    articles = [str(a).strip() for a in (raw.get("articles") or []) if str(a).strip()]

    return Settings(
        region=str(raw.get("region", "Пермский край")),
        city=str(raw.get("city", "Пермь")),
        court_type=str(raw.get("court_type", "Мировой судья")),
        default_articles=articles,
        districts=[str(d) for d in (raw.get("districts") or [])],
        lookback_days=int(raw.get("lookback_days", 7)),
        scraper=scraper,
        db_path=os.getenv("DB_PATH", "data/pravosudie.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
