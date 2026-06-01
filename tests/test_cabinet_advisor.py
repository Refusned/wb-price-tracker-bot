"""Тест CabinetAdvisor. Фейковые InsightEngine и LLM — без сети.
Проверяем: сводка кормит LLM ключевыми цифрами/сигналами, и возвращается
текст модели.
"""
from __future__ import annotations

import math
from typing import Any, cast

import pytest

from app.services.cabinet_advisor import CabinetAdvisor
from app.services.insight_engine import BriefingData, Insight, InsightEngine
from app.storage.business_repository import DailyMetrics

pytestmark = pytest.mark.asyncio


def _dm(sales: int = 0, returns: int = 0, orders: int = 0) -> DailyMetrics:
    return DailyMetrics(
        date="2026-06-01", orders_count=orders, orders_canceled=0,
        sales_count=sales, returns_count=returns, revenue_total=float(sales * 1000),
        revenue_net=float(sales * 850), unique_articles=1, buyout_rate=80.0,
    )


def _briefing() -> BriefingData:
    return BriefingData(
        yesterday=_dm(sales=5), today=_dm(sales=3, orders=4), week=_dm(sales=28, returns=2),
        velocity=4.0, total_stock=6, in_way_to_client=0, days_left=1.5,
        market_min_price=15990.0, recommended_buy_count=50,
        insights=[Insight(level="critical", emoji="🔴",
                          title="Остатков на 1.5 дн", body="Скоро out-of-stock.",
                          action="Закупи в ближайшие сутки")],
    )


class FakeInsights:
    def __init__(self, briefing: BriefingData) -> None:
        self._b = briefing

    async def build_briefing(self) -> BriefingData:
        return self._b


class FakeLLM:
    def __init__(self, reply: str = "Главное: остатки кончаются.") -> None:
        self._reply = reply
        self.last_user = ""

    async def generate(self, *, system: str, user: str, **kw: Any) -> str:
        self.last_user = user
        return self._reply


async def test_build_advice_feeds_numbers_and_returns_llm_text() -> None:
    llm = FakeLLM(reply="Срочно пополни остатки и снизь возвраты.")
    advisor = CabinetAdvisor(
        insight_engine=cast(InsightEngine, FakeInsights(_briefing())),
        llm_client=cast(Any, llm),
    )
    out = await advisor.build_advice()

    assert out == "Срочно пополни остатки и снизь возвраты."
    # сводка содержит ключевые цифры и сигнал
    assert "4.0 шт/день" in llm.last_user
    assert "6 шт" in llm.last_user
    assert "1.5 дн" in llm.last_user
    assert "15990₽" in llm.last_user
    assert "Остатков на 1.5 дн" in llm.last_user


async def test_serialize_handles_infinite_days_and_no_market_price() -> None:
    b = _briefing()
    b.days_left = math.inf
    b.market_min_price = None
    b.insights = []
    text = CabinetAdvisor._serialize(b)

    assert "∞ дн" in text
    assert "н/д" in text
    assert "Сигналы бота" not in text  # пустой список — секции нет
