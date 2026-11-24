"""Тесты извлечения кода капчи из ответа vision-модели."""
from __future__ import annotations

from src.captcha_ai import extract_code


def test_extract_code_plain_word():
    assert extract_code("ажур") == "ажур"


def test_extract_code_quoted():
    assert extract_code("Код: «ёж»") == "ёж"


def test_extract_code_skips_noise_words():
    assert extract_code("На картинке капча, код: сноп") == "сноп"


def test_extract_code_mixed_cyrillic_latin():
    assert extract_code("Ответ: кот12") == "кот12"


def test_extract_code_fallback_strips_to_allowed():
    # без кавычек и с лишними знаками — склеиваем разрешённые символы
    assert extract_code("к-о-т") == "кот"


def test_extract_code_empty():
    assert extract_code("") == ""
    assert extract_code(None) == ""
