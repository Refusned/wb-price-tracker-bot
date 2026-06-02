"""build_stock_message: футер диагностики свежести (/stock)."""
from __future__ import annotations

from app.utils.business_formatting import build_stock_message

_STOCKS = [
    {"supplier_article": "A-1", "subject": "X", "total_qty": 5,
     "total_in_way_to": 0, "warehouse_count": 1},
]


def test_footer_present_with_meta() -> None:
    msg = build_stock_message(
        _STOCKS, last_sync_at="2026-06-01T00:00:00", fbo_count=3, fbs_count=0
    )
    assert "🔄 Синхронизация" in msg
    assert "FBO 3" in msg and "FBS 0" in msg and "артикулов 1" in msg


def test_footer_absent_without_meta() -> None:
    # Обратная совместимость: без метаданных футера нет.
    assert "Синхронизация" not in build_stock_message(_STOCKS)


def test_footer_partial_meta_only_fbs() -> None:
    # Выборочная передача: только fbs_count → блок FBO отсутствует.
    msg = build_stock_message(_STOCKS, fbs_count=2)
    assert "FBS 2" in msg
    assert "FBO" not in msg
