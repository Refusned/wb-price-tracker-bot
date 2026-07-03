"""Тесты общих форматтеров (формат чисел/дат/обрезки — витрина бота)."""
from __future__ import annotations

from app.utils.formatting import (
    format_iso_datetime,
    format_percent,
    format_price_rub,
    shorten,
)


def test_format_percent_strips_trailing_zero() -> None:
    assert format_percent(24.0) == "24%"
    assert format_percent(24) == "24%"


def test_format_percent_keeps_fraction() -> None:
    assert format_percent(24.5) == "24.5%"
    assert format_percent(0.25) == "0.25%"


def test_shorten_short_text_untouched() -> None:
    assert shorten("Кабель", 30) == "Кабель"
    assert shorten("ровно десять", 12) == "ровно десять"  # точный лимит — без «…»


def test_shorten_long_text_gets_ellipsis() -> None:
    out = shorten("Очень длинное название товара на WB", 20)
    assert out.endswith("…")
    assert len(out) <= 20


def test_format_iso_datetime_no_seconds() -> None:
    assert format_iso_datetime("2026-07-03T10:30:45") == "2026-07-03 10:30"


def test_format_iso_datetime_fallbacks() -> None:
    assert format_iso_datetime(None) == "нет данных"
    assert format_iso_datetime("мусор") == "мусор"  # битое значение — как есть


def test_format_price_rub_thousands_space() -> None:
    assert format_price_rub(12345) == "12 345 ₽"
