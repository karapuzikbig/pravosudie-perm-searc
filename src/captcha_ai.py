"""Распознавание KCAPTCHA через vision-модель (облако) как тяжёлая артиллерия.

Используется, когда локальный OCR (ddddocr) не даёт уверенного результата.
Работает по OpenAI-совместимому Chat Completions API (image_url в base64),
поэтому подходит и для OpenAI, и для любых совместимых шлюзов (в т.ч.
локальных), если указать базовый URL и модель в config.yaml.

Ключ берётся из переменной окружения VISION_API_KEY (или OPENAI_API_KEY).
Если ключа нет — модуль просто не срабатывает, и остаётся локальный OCR.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_PROMPT = (
    "На картинке капча (KCAPTCHA): латинские буквы и цифры, искажённые волнами "
    "и шумом. Верни ТОЛЬКО символы капчи без пробелов, знаков препинания и "
    "пояснений. Если не уверен — верни наиболее вероятный вариант."
)


def solve_captcha_vision(
    img_bytes: bytes,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1",
    timeout: float = 30.0,
) -> Optional[str]:
    """Отправить картинку капчи в vision-модель, вернуть распознанный код.

    Возвращает строку только из букв/цифр либо None при ошибке/без ключа.
    """
    if not api_key:
        log.info("Ключ vision-модели не задан (VISION_API_KEY) — пропускаю")
        return None
    if not img_bytes:
        return None

    b64 = base64.b64encode(img_bytes).decode("ascii")
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 20,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ],
            }
        ],
    }
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("Vision-запрос к капче не удался: %s", exc)
        return None

    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        log.warning("Неожиданный ответ vision-модели: %s", str(body)[:200])
        return None
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    code = "".join(ch for ch in (text or "") if ch.isalnum())
    if not code:
        log.info("Vision-модель не вернула читаемых символов")
        return None
    log.info("Vision-модель распознала капчу: «%s»", code)
    return code
