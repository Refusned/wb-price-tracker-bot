"""HTML-шаблоны бизнес-блока: экранирование WB-строк и парные <b>-теги."""
from __future__ import annotations

from html.parser import HTMLParser

from app.utils.business_formatting import (
    build_abc_message,
    build_new_order_alert,
    build_profit_message,
    build_stock_message,
)

_EVIL_SUBJECT = 'Кабель <b>&test</b> "USB"'


def _assert_balanced_html(text: str) -> None:
    """Разметка валидна: парные теги, никаких висячих <b>."""

    class _Checker(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.stack: list[str] = []

        def handle_starttag(self, tag: str, attrs: list) -> None:
            self.stack.append(tag)

        def handle_endtag(self, tag: str) -> None:
            assert self.stack and self.stack[-1] == tag, f"непарный </{tag}>"
            self.stack.pop()

    checker = _Checker()
    checker.feed(text)
    assert checker.stack == [], f"незакрытые теги: {checker.stack}"


def test_stock_escapes_subject() -> None:
    msg = build_stock_message([
        {"supplier_article": "A-1", "subject": _EVIL_SUBJECT, "total_qty": 5,
         "total_in_way_to": 0, "warehouse_count": 1},
    ])
    assert "&lt;b&gt;" in msg          # спецсимволы из WB экранированы
    assert "<b>&test" not in msg       # сырой инъекции нет
    _assert_balanced_html(msg)


def test_order_alert_escapes_warehouse_and_subject() -> None:
    msg = build_new_order_alert({
        "supplier_article": "019", "nm_id": 111, "subject": _EVIL_SUBJECT,
        "price_with_disc": 15000, "warehouse_name": "Коледино <север>",
        "date": "2026-07-03T10:00:00",
    })
    assert "&lt;север&gt;" in msg
    _assert_balanced_html(msg)


def test_profit_message_has_bold_total_and_separator() -> None:
    msg = build_profit_message({
        "total_sold": 10, "total_returns": 1, "gross_for_pay": 100000.0,
        "gross_total_price": 120000.0, "uncovered_revenue": 0, "tax_percent": 4.0,
        "acquiring_percent": 0.0, "matched_sold": 10, "unmatched_sold": 0,
        "total_tax": 4800.0, "total_logistics": 1820.0, "total_revenue": 93380.0,
        "total_cost": 80000.0, "total_profit": 13380.0, "margin_pct": 13.4,
        "roi_pct": 16.7, "breakdown": [], "missing_purchase_data": [],
    }, "7 дней")
    assert "<b>Чистая прибыль: 13 380 ₽</b>" in msg
    assert "━━━" in msg                 # разделитель перед итогом
    assert "налог УСН 4%" in msg        # format_percent без хвоста .0
    _assert_balanced_html(msg)


def test_abc_empty_state_friendly() -> None:
    msg = build_abc_message([])
    assert msg.startswith("📊")
    assert "пока нет" in msg
