"""🔴 Центральный money-safety тест AgentToolset (Фаза 3).

Главный инвариант MS-1: ни один read- или propose-инструмент НЕ вызывает
мутирующих методов (add_purchase / set_value / answer_*). Проверяем по факту:
у фейков write-методы пишут в общий список writes; после прогона ВСЕХ
инструментов список обязан быть пуст. (call() глушит исключения и возвращает
ошибку строкой, поэтому raise в фейке не годится как сигнал — нужен список.)
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from app.services.agent_tools import AgentToolset
from app.storage.business_repository import DailyMetrics
from app.wb.feedbacks_client import Feedback, Question

pytestmark = pytest.mark.asyncio

WRITES: list[str] = []


class FakeBiz:
    async def get_period_metrics(self, days: int) -> DailyMetrics:
        return DailyMetrics(f"last-{days}d", 10, 1, 8, 1, 8000.0, 6000.0, 3, 80.0)

    async def get_daily_metrics(self, date: str) -> DailyMetrics:
        return DailyMetrics(date, 2, 0, 2, 0, 2000.0, 1500.0, 1, 100.0)

    async def get_abc_analysis(self, days: int = 30) -> list[dict]:
        return [{"nm_id": 111, "supplier_article": "ART-1", "subject": "Куртка",
                 "sale_count": 5, "net_revenue": 5000.0, "returns": 1, "avg_for_pay": 1000.0}]

    async def get_profit_breakdown(self, days: Any = None, **kw: Any) -> list[dict]:
        return [{"supplier_article": "ART-1", "nm_id": 111, "subject": "Куртка",
                 "sold_qty": 5, "returns_qty": 1, "revenue": 3000.0, "profit": 1000.0,
                 "margin_pct": 33.3, "roi_pct": 50.0, "avg_buy_price": 400.0,
                 "has_purchase_data": True}]

    async def get_total_profit(self, days: Any = None, **kw: Any) -> dict:
        return {"total_profit": 1000.0, "margin_pct": 20.0,
                "missing_purchase_data": [], "breakdown": [{"x": 1}]}

    async def get_stock_summary(self) -> list[dict]:
        return [{"nm_id": 111, "supplier_article": "ART-1", "subject": "Куртка",
                 "total_qty": 18, "total_in_way_to": 5, "total_in_way_from": 0,
                 "warehouse_count": 2, "last_update": "2026-06-02"}]

    async def get_returns(self, days: int = 30, limit: int = 20) -> list[dict]:
        return [{"srid": "S1", "date": "2026-06-01", "nm_id": 111,
                 "supplier_article": "ART-1", "subject": "Куртка",
                 "total_price": 1000.0, "for_pay": 800.0, "warehouse_name": "Коледино"}]

    # WRITE — НЕ должны вызываться из toolset
    async def add_purchase(self, *a: Any, **k: Any) -> int:
        WRITES.append("add_purchase")
        return 1

    async def upsert_orders(self, *a: Any, **k: Any) -> list:
        WRITES.append("upsert_orders")
        return []


class FakeSettings:
    async def get_float(self, key: str, default: float) -> float:
        return default

    async def set_value(self, *a: Any, **k: Any) -> None:
        WRITES.append("set_value")

    async def set_float(self, *a: Any, **k: Any) -> None:
        WRITES.append("set_float")


class FakeSeller:
    def __init__(self) -> None:
        self.calls = 0

    async def get_nm_report_detail(self, nm_ids: list[int], date_from: Any, date_to: Any) -> list[dict]:
        self.calls += 1
        return [{"nmID": 111, "vendorCode": "ART-1", "statistics": {"selectedPeriod": {
            "openCardCount": 100, "addToCartCount": 20, "ordersCount": 8,
            "buyoutsCount": 6, "buyoutPercent": 75}}}]


class FakeFeedbacks:
    async def get_unanswered_feedbacks(self, **kw: Any) -> list[Feedback]:
        return [Feedback(id="F1", text="отличный товар", rating=5, created_date="",
                         nm_id=111, product_name="Куртка", user_name="Иван")]

    async def get_unanswered_questions(self, **kw: Any) -> list[Question]:
        return [Question(id="Q1", text="когда доставка?", created_date="",
                         nm_id=111, product_name="Куртка")]

    async def answer_feedback(self, *a: Any, **k: Any) -> None:
        WRITES.append("answer_feedback")

    async def answer_question(self, *a: Any, **k: Any) -> None:
        WRITES.append("answer_question")


def _toolset() -> tuple[AgentToolset, FakeSeller]:
    seller = FakeSeller()
    ts = AgentToolset(
        business_repository=FakeBiz(),  # type: ignore[arg-type]
        settings_repository=FakeSettings(),  # type: ignore[arg-type]
        seller_client=seller,  # type: ignore[arg-type]
        feedbacks_client=FakeFeedbacks(),  # type: ignore[arg-type]
    )
    return ts, seller


# Полный набор вызовов: каждый read- и propose-инструмент c валидными аргументами.
_EXERCISE = [
    ("get_period_summary", {}),
    ("get_daily_metrics", {"date": "today"}),
    ("get_daily_metrics", {"date": "yesterday"}),
    ("get_top_articles", {}),
    ("get_profit_breakdown", {}),
    ("get_total_profit", {}),
    ("get_stock_summary", {}),
    ("get_returns", {}),
    ("get_funnel", {}),
    ("get_unanswered_feedbacks", {}),
    ("get_unanswered_questions", {}),
    ("propose_purchase", {"nm_id": 111, "quantity": 10, "buy_price_per_unit": 500}),
    ("propose_profit_setting", {"param": "tax", "value": 3}),
    ("propose_feedback_reply", {"target_id": "F1", "kind": "feedback",
                                "text": "Спасибо большое за тёплый отзыв!"}),
]


async def test_no_tool_triggers_any_write() -> None:
    WRITES.clear()
    ts, _ = _toolset()
    ts.new_turn()
    for name, args in _EXERCISE:
        out = await ts.call(name, args)
        json.loads(out)  # валидный JSON
    assert WRITES == [], f"toolset вызвал мутацию(и): {WRITES}"


async def test_registry_matches_allowlist() -> None:
    ts, _ = _toolset()
    assert set(ts.tool_names()) == {
        "get_period_summary", "get_daily_metrics", "get_top_articles",
        "get_profit_breakdown", "get_total_profit", "get_stock_summary",
        "get_returns", "get_funnel", "get_unanswered_feedbacks",
        "get_unanswered_questions", "propose_purchase", "propose_profit_setting",
        "propose_feedback_reply",
    }


async def test_optional_tools_absent_without_clients() -> None:
    ts = AgentToolset(
        business_repository=FakeBiz(),  # type: ignore[arg-type]
        settings_repository=FakeSettings(),  # type: ignore[arg-type]
        seller_client=None, feedbacks_client=None,
    )
    names = set(ts.tool_names())
    assert "get_funnel" not in names               # нет seller_client
    assert "get_unanswered_feedbacks" not in names  # нет feedbacks_client
    assert "propose_feedback_reply" not in names
    assert "propose_purchase" in names              # локальные — всегда
    assert "propose_profit_setting" in names


async def test_read_tools_return_expected_shapes() -> None:
    ts, _ = _toolset()
    period = json.loads(await ts.call("get_period_summary", {"days": 7}))
    assert period["sales_count"] == 8 and period["buyout_rate"] == 80.0
    total = json.loads(await ts.call("get_total_profit", {}))
    assert "breakdown" not in total  # тяжёлый ключ выкинут
    assert total["total_profit"] == 1000.0
    funnel = json.loads(await ts.call("get_funnel", {}))
    assert funnel["funnel"][0]["buyout_percent"] == 75


async def test_get_funnel_memoized_per_turn() -> None:
    ts, seller = _toolset()
    ts.new_turn()
    await ts.call("get_funnel", {"nm_ids": [111], "days": 7})
    await ts.call("get_funnel", {"nm_ids": [111], "days": 7})
    assert seller.calls == 1  # один реальный WB-вызов на повторы за ход
    ts.new_turn()
    await ts.call("get_funnel", {"nm_ids": [111], "days": 7})
    assert seller.calls == 2  # новый ход — кэш сброшен


async def test_propose_purchase_validates_article_and_amounts() -> None:
    ts, _ = _toolset()
    ok = json.loads(await ts.call("propose_purchase",
                                  {"nm_id": 111, "quantity": 10, "buy_price_per_unit": 500}))
    assert ok["ok"] is True and ok["kind"] == "purchase"
    assert ok["params"]["quantity"] == 10 and ok["params"]["supplier_article"] == "ART-1"

    bad_art = json.loads(await ts.call("propose_purchase",
                                       {"nm_id": 999, "quantity": 1, "buy_price_per_unit": 5}))
    assert bad_art["ok"] is False  # неизвестный артикул

    bad_qty = json.loads(await ts.call("propose_purchase",
                                       {"nm_id": 111, "quantity": 0, "buy_price_per_unit": 5}))
    assert bad_qty["ok"] is False  # qty <= 0


async def test_propose_profit_setting_range() -> None:
    ts, _ = _toolset()
    ok = json.loads(await ts.call("propose_profit_setting", {"param": "tax", "value": 3}))
    assert ok["ok"] is True and ok["params"]["settings_key"] == "profit_tax_percent"
    bad = json.loads(await ts.call("propose_profit_setting", {"param": "tax", "value": 99}))
    assert bad["ok"] is False  # вне [0;50]
    bad2 = json.loads(await ts.call("propose_profit_setting", {"param": "wat", "value": 1}))
    assert bad2["ok"] is False  # неизвестный param


async def test_propose_feedback_reply_content_gate() -> None:
    ts, _ = _toolset()
    ok = json.loads(await ts.call("propose_feedback_reply",
                                  {"target_id": "F1", "kind": "feedback",
                                   "text": "Спасибо за отзыв, рады что понравилось!"}))
    assert ok["ok"] is True and ok["params"]["target_kind"] == "feedback"
    phone = json.loads(await ts.call("propose_feedback_reply",
                                     {"target_id": "F1", "kind": "feedback",
                                      "text": "Звоните +7 900 123 45 67"}))
    assert phone["ok"] is False  # контент-гейт: телефон
