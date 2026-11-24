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
имени модели (``gpt://<folder_id>/<vision-модель>/latest``), либо
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

_PROMPT = (
    "На картинке капча (KCAPTCHA): РУССКИЕ строчные буквы (кириллица), искажённые "
    "волнами и шумом. Верни ТОЛЬКО эти русские буквы без пробелов, знаков "
    "препинания, цифр и пояснений. Не заменяй русские буквы похожими латинскими — "
    "пиши именно кириллицей. Если не уверен — верни наиболее вероятный вариант."
)

# Код капчи портала — короткое «слово» из букв/цифр (по наблюдениям капча на
# портале состоит из строчных РУССКИХ букв). Разрешаем и кириллицу, и латиницу:
# модель иногда отвечает латиницей вместо похожих русских букв.
_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
)
_WORD = "".join(sorted(_ALLOWED))
_NOISE_WORDS = {
    "капча", "картинке", "картинка", "код", "буквы", "буква", "символы", "символ",
    "русские", "кириллица", "текст", "ответ", "изображение", "изображении",
}


def extract_code(text: str | None) -> str:
    """Извлечь код капчи из ответа модели.

    Ищем короткое слово (4–8 символов) из букв/цифр; отбрасываем служебные
    русские слова («капча», «буквы»…), которые модель может дописать.
    """
    import re  # noqa: PLC0415

    if not text:
        return ""
    text = text.strip()
    for pattern in (
        rf"[«\"'“]([{_WORD}]{{2,8}})[»\"'”]",
        rf"\b([{_WORD}]{{4,8}})\b",
    ):
        for m in re.finditer(pattern, text):
            cand = m.group(1)
            if cand.lower() not in _NOISE_WORDS:
                return cand
    return "".join(ch for ch in text if ch in _ALLOWED)


def solve_captcha_vision(
    img_bytes: bytes,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    base_url: str = "https://api.openai.com/v1",
    auth_scheme: str = "bearer",
    folder_id: str = "",
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
        # тело ответа обычно содержит причину (особенно у Yandex Cloud)
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            detail = ""
        log.warning(
            "Vision-запрос к капче не удался: HTTP %s %s. auth_scheme=%s, "
            "модель=%s. Ответ: %s",
            exc.code,
            exc.reason,
            auth_scheme,
            model,
            detail[:500] or "(пусто)",
        )
        return None
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
    log.info("Vision-модель ответила: %r", (text or "")[:200])
    code = extract_code(text)
    if not code:
        log.info("Vision-модель не вернула читаемых символов")
        return None
    log.info("Vision-модель распознала капчу: «%s»", code)
    return code


def probe_vision(
    img_bytes: bytes,
    *,
    api_key: str,
    model: str,
    base_url: str = "https://llm.api.cloud.yandex.net/v1",
    auth_scheme: str = "api-key",
    folder_id: str = "",
    timeout: float = 30.0,
) -> None:
    """Отправить капчу в модель и напечатать СЫРОЙ ответ/ошибку (для отладки)."""
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
    headers = {
        "Content-Type": "application/json",
        "Authorization": (
            f"Api-Key {api_key}" if auth_scheme == "api-key" else f"Bearer {api_key}"
        ),
    }
    if folder_id:
        headers["x-folder-id"] = folder_id
    data = json.dumps(payload).encode("utf-8")
    print("URL:", url)
    print("model:", model, "| auth:", auth_scheme, "| folder:", folder_id or "-")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            print("HTTP", resp.status)
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        print("HTTP ERROR", exc.code, exc.reason)
        try:
            print(exc.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        print("ERROR:", exc)


if __name__ == "__main__":
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    if len(sys.argv) < 2:
        print("usage: python -m src.captcha_ai <captcha.png>")
        raise SystemExit(2)
    import os  # noqa: PLC0415

    probe_vision(
        Path(sys.argv[1]).read_bytes(),
        api_key=os.getenv("VISION_API_KEY", "") or os.getenv("OPENAI_API_KEY", ""),
        model=os.getenv("VISION_MODEL", ""),
        base_url=os.getenv("VISION_BASE_URL", "https://llm.api.cloud.yandex.net/v1"),
        auth_scheme=os.getenv("VISION_AUTH_SCHEME", "api-key"),
        folder_id=os.getenv("YANDEX_FOLDER_ID", ""),
    )
