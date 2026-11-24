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
    captcha_wait_seconds: int = 120
    captcha_mode: str = "auto"  # auto | manual
    captcha_max_attempts: int = 8
    screenshot_on_error: bool = True
    # vision-модель как «тяжёлая артиллерия», если локальный OCR не справляется
    captcha_vision_enabled: bool = True
    captcha_vision_model: str = "gpt://<folder_id>/qwen2.5-vl-32b-instruct/latest"
    captcha_vision_base_url: str = "https://api.openai.com/v1"
    captcha_vision_auth_scheme: str = "bearer"  # bearer | api-key (Yandex API-ключ)
    captcha_vision_folder_id: str = ""  # Yandex Cloud: id каталога (x-folder-id)
    captcha_vision_max_tokens: int = 512  # запас на reasoning-модели
    captcha_vision_reasoning_effort: str = "none"  # none отключает reasoning (быстрее)
    # авто-перезагрузка при ошибке 500 на странице участка
    retry_on_500: bool = True
    max_500_reloads: int = 5
    output_excel: str = "data/cases.xlsx"


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
    vision_api_key: str = ""

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
        captcha_wait_seconds=int(scraper_raw.get("captcha_wait_seconds", 120)),
        captcha_mode=str(scraper_raw.get("captcha_mode", "auto")).lower(),
        captcha_max_attempts=int(scraper_raw.get("captcha_max_attempts", 8)),
        screenshot_on_error=bool(scraper_raw.get("screenshot_on_error", True)),
        captcha_vision_enabled=bool(scraper_raw.get("captcha_vision_enabled", True)),
        captcha_vision_model=str(scraper_raw.get("captcha_vision_model", "gpt-4o-mini")),        captcha_vision_base_url=str(
            scraper_raw.get("captcha_vision_base_url", "https://api.openai.com/v1")
        ),
        captcha_vision_auth_scheme=str(
            scraper_raw.get("captcha_vision_auth_scheme", "bearer")
        ).lower(),
        captcha_vision_folder_id=str(
            scraper_raw.get("captcha_vision_folder_id", "")
            or os.getenv("YANDEX_FOLDER_ID", "")
        ),
        captcha_vision_max_tokens=int(scraper_raw.get("captcha_vision_max_tokens", 512)),
        captcha_vision_reasoning_effort=str(
            scraper_raw.get("captcha_vision_reasoning_effort", "none")
        ).lower(),
        retry_on_500=bool(scraper_raw.get("retry_on_500", True)),
        max_500_reloads=int(scraper_raw.get("max_500_reloads", 5)),
        output_excel=str(
            scraper_raw.get("output_excel", "")
            or os.getenv("EXCEL_PATH", "data/cases.xlsx")
        ),
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
        vision_api_key=os.getenv("VISION_API_KEY", "") or os.getenv("OPENAI_API_KEY", ""),
    )
