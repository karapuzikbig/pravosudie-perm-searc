"""Точка входа: запускает окно программы (tkinter)."""
from __future__ import annotations

import logging

from .config import BASE_DIR, load_settings
from .gui import run


def main() -> None:
    settings = load_settings()
    level = getattr(logging, settings.log_level, logging.INFO)
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_dir / "app.log", encoding="utf-8"),
        ],
    )
    logging.getLogger(__name__).info("Лог пишется в %s", log_dir / "app.log")
    run(settings)


if __name__ == "__main__":
    main()
