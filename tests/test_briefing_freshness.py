"""Part B: штамп свежести остатков в утреннем брифинге.

Дайджест показывает время снимка остатков по МСК, чтобы продавец мог сверить
«Остаток» с кабинетом на конкретный момент (остаток ≤30 мин из-за лага WB).
"""
from __future__ import annotations

from app.services.insight_engine import BriefingData
from app.storage.business_repository import DailyMetrics
from app.utils.business_formatting import build_briefing_message


def _dm() -> DailyMetrics:
    return DailyMetrics(
        date="2026-06-24", orders_count=0, orders_canceled=0,
        sales_count=0, returns_count=0, revenue_total=0.0,
        revenue_net=0.0, unique_articles=0, buyout_rate=0.0,
    )


def _briefing(stock_synced_at: str | None) -> BriefingData:
    return BriefingData(
        yesterday=_dm(), today=_dm(), week=_dm(),
        velocity=0.0, total_stock=21, in_way_to_client=10, days_left=float("inf"),
        market_min_price=None, recommended_buy_count=0, insights=[],
        stock_synced_at=stock_synced_at,
    )


def test_briefing_shows_msk_freshness_stamp() -> None:
    # 17:05 UTC → 20:05 МСК (UTC+3, без перехода)
    msg = build_briefing_message(_briefing("2026-06-24T17:05:00+00:00"))
    assert "обновлено: 24.06 20:05 МСК" in msg


def test_briefing_omits_stamp_when_unknown() -> None:
    msg = build_briefing_message(_briefing(None))
    assert "обновлено" not in msg
