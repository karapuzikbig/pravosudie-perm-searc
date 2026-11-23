"""Загрузка и валидация конфигурации (config.yaml + .env)."""
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
    articles: list[str]
    region: str
    city: str
    court_type: str
    districts: list[str] = field(default_factory=list)
    lookback_days: int = 7
    poll_interval_minutes: int = 60
    notify_channel: str = "telegram"
    notify_chat_id: int | None = None
    scraper: ScraperConfig = field(default_factory=ScraperConfig)

    # из .env
    tg_bot_token: str = ""
    admin_chat_id: int | None = None
    db_path: str = "data/pravosudie.db"
    log_level: str = "INFO"

    def effective_chat_id(self) -> int | None:
        return self.admin_chat_id or self.notify_chat_id


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
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    articles = [str(a).strip() for a in (raw.get("articles") or []) if str(a).strip()]
    if not articles:
        raise ValueError("config.yaml: список 'articles' пуст — укажите хотя бы одну статью КоАП")

    scraper_raw = raw.get("scraper") or {}
    scraper = ScraperConfig(
        headless=bool(scraper_raw.get("headless", True)),
        timeout_ms=int(scraper_raw.get("timeout_ms", 30000)),
        delay_min_between_requests=float(scraper_raw.get("delay_min_between_requests", 1.5)),
        delay_max_between_requests=float(scraper_raw.get("delay_max_between_requests", 3.5)),
        max_pages=int(scraper_raw.get("max_pages", 20)),
        screenshot_on_error=bool(scraper_raw.get("screenshot_on_error", True)),
    )

    notify_raw = raw.get("notify") or {}

    settings = Settings(
        articles=articles,
        region=str(raw.get("region", "Пермский край")),
        city=str(raw.get("city", "Пермь")),
        court_type=str(raw.get("court_type", "Мировой судья")),
        districts=[str(d) for d in (raw.get("districts") or [])],
        lookback_days=int(raw.get("lookback_days", 7)),
        poll_interval_minutes=int(raw.get("poll_interval_minutes", 60)),
        notify_channel=str(notify_raw.get("channel", "telegram")),
        notify_chat_id=_as_int(notify_raw.get("chat_id")),
        scraper=scraper,
        tg_bot_token=os.getenv("TG_BOT_TOKEN", "").strip(),
        admin_chat_id=_as_int(os.getenv("ADMIN_CHAT_ID")),
        db_path=os.getenv("DB_PATH", "data/pravosudie.db"),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
    return settings
