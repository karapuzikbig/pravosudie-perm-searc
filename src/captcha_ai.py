"""Распознавание KCAPTCHA через vision-модель (облако) как тяжёлая артиллерия.

Используется, когда локальный OCR (ddddocr) не даёт уверенного результата.
Работает по OpenAI-совместимому Chat Completions API (image_url в base64),
поэтому подходит и для OpenAI, и для любых совместимых шлюзов (в т.ч.
локальных), а также для Yandex Cloud Foundation Models.

Авторизация настраивается через ``auth_scheme``:
  * "bearer"  — ``Authorization: Bearer <ключ>`` (OpenAI, OpenRouter,
    и Yandex Cloud при IAM-токене);
  * "api-key" — ``Authorization: Api-Key <ключ>`` (Yandex API-ключ
    сервисного аккаунта).

Для Yandex IAM-токен живёт ~12 часов, поэтому его удобно брать из
переменной окружения / файла, который периодически обновляют
(``yc iam create-token``). Идентификатор каталога задаётся либо прямо в
имени модели (``gpt://<folder_id>/qwen2.5-vl-32b-instruct/latest``), либо
заголовком ``x-folder-id`` (``folder_id``).

Ключ берётся из переменной окружения VISION_API_KEY (или OPENAI_API_KEY).
Если ключа нет — модуль просто не срабатывает, и остаётся локальный OCR.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_RU_LETTERS = set("абвгдеёжзийклмнопрстуфхцчшщъыьэюя")

_PROMPT = (
    "На картинке капча (KCAPTCHA) из русских букв (кириллица), искажённых "
    "волнами и шумом. Верни ТОЛЬКО русские буквы капчи в том регистре, в "
    "каком они изображены, без пробелов, знаков препинания и пояснений. "
    "Не заменяй русские буквы латинскими. Если не уверен — верни наиболее "
    "вероятный вариант."
)



def solve_captcha_vision(
    img_bytes: bytes,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1",
    auth_scheme: str = "bearer",
    folder_id: str = "",
    max_tokens: int = 512,
    reasoning_effort: str = "none",
    timeout: float = 30.0,
) -> Optional[str]:
    """Отправить картинку капчи в vision-модель, вернуть распознанный код.

    Возвращает строку только из букв/цифр либо None при ошибке/без ключа.
    ``auth_scheme``: "bearer" (по умолчанию) или "api-key" (Yandex API-ключ).
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
        # Небольшой лимит: ответ — только символы капчи. Но reasoning-моделям
        # (например, Yandex Qwen) нужно место на «размышления», иначе content
        # остаётся пустым.
        "max_tokens": max_tokens,
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
    # Для распознавания капчи многошаговое рассуждение не нужно: отключаем,
    # чтобы ответ приходил сразу в content (а не в reasoning_content) и не
    # обрезался лимитом токенов. Yandex понимает "none" у Qwen3.6.
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")

    if auth_scheme == "api-key":
        auth_header = f"Api-Key {api_key}"
    else:
        auth_header = f"Bearer {api_key}"
    headers = {
        "Content-Type": "application/json",
        "Authorization": auth_header,
    }
    if folder_id:
        headers["x-folder-id"] = folder_id

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            detail = ""
        log.warning("Vision-модель ответила HTTP %s: %s", exc.code, detail)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Vision-запрос к капче не удался: %s", exc)
        return None

    choice = {}
    try:
        choice = body["choices"][0] or {}
    except (KeyError, IndexError, TypeError):
        log.warning("Неожиданный ответ vision-модели: %s", json.dumps(body, ensure_ascii=False)[:500])
        return None
    message = choice.get("message") or {}
    text = message.get("content") or ""
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    if not (text or "").strip():
        # reasoning-модели кладут «размышления» отдельно, а content может быть
        # пустым (обрезан лимитом или модель не поддерживает картинки).
        reasoning = message.get("reasoning_content") or ""
        finish = choice.get("finish_reason")
        log.warning(
            "Vision-модель вернула пустой ответ (finish_reason=%s, reasoning=%r). "
            "Проверьте, что модель — vision (vl) и она поддерживает изображения.",
            finish,
            (reasoning or "")[:200],
        )
        return None
    code = "".join(ch for ch in text if ch.lower() in _RU_LETTERS)
    if not code:
        log.info(
            "Vision-модель не вернула русских букв (ответ: %r); "
            "капча состоит только из кириллицы",
            text[:100],
        )
        return None
    log.info("Vision-модель распознала капчу: «%s»", code)
    return code
