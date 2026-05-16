from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from app.services.stock_arrival_detector import StockArrivalDetector
from app.storage.business_repository import BusinessRepository
from app.storage.db import Database
from app.storage.repositories import SubscriberRepository
from app.storage.stock_arrival_repository import StockArrivalRepository
from app.wb.seller_client import StockEntry


pytestmark = pytest.mark.asyncio


class FakeSubscriberRepository:
    def __init__(self, chat_ids: list[int]) -> None:
        self._chat_ids = chat_ids

    async def list_active_chat_ids(self) -> list[int]:
        return self._chat_ids


async def make_detector(
    tmp_path: Path,
    *,
    delta_threshold: int = 5,
    chat_ids: list[int] | None = None,
) -> tuple[Database, BusinessRepository, StockArrivalRepository, StockArrivalDetector, AsyncMock]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()

    business_repo = BusinessRepository(db)
    stock_arrival_repo = StockArrivalRepository(db)
    bot = AsyncMock(spec=Bot)
    subscriber_repo = FakeSubscriberRepository(chat_ids if chat_ids is not None else [123])

    detector = StockArrivalDetector(
        repository=stock_arrival_repo,
        business_repository=business_repo,
        subscriber_repository=cast(SubscriberRepository, subscriber_repo),
        bot=cast(Bot, bot),
        delta_threshold=delta_threshold,
    )
    return db, business_repo, stock_arrival_repo, detector, bot


async def upsert_stock(
    business_repo: BusinessRepository,
    *,
    nm_id: int,
    quantity: int,
    in_way_to_client: int = 0,
    in_way_from_client: int = 0,
    supplier_article: str | None = None,
    warehouse_name: str = "WB",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await business_repo.upsert_stocks(
        [
            StockEntry(
                nm_id=nm_id,
                supplier_article=supplier_article or f"A-{nm_id}",
                warehouse_name=warehouse_name,
                quantity=quantity,
                in_way_to_client=in_way_to_client,
                in_way_from_client=in_way_from_client,
                quantity_full=quantity + in_way_to_client + in_way_from_client,
                subject="Тестовый товар",
                last_change_date=now,
            )
        ],
        now,
    )


async def test_scan_empty_stocks_returns_zero(tmp_path: Path) -> None:
    db, _, _, detector, bot = await make_detector(tmp_path)
    try:
        created = await detector.scan()

        assert created == 0
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_scan_first_run_sets_baseline_no_prompts(tmp_path: Path) -> None:
    db, business_repo, stock_arrival_repo, detector, bot = await make_detector(tmp_path)
    try:
        await upsert_stock(business_repo, nm_id=100, quantity=10)

        created = await detector.scan()

        baselines = await stock_arrival_repo.get_baselines()
        assert created == 0
        assert baselines[100]["last_total_full"] == 10
        assert await stock_arrival_repo.count_pending() == 0
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_scan_positive_delta_above_threshold_creates_prompt(tmp_path: Path) -> None:
    db, business_repo, stock_arrival_repo, detector, bot = await make_detector(tmp_path)
    try:
        await upsert_stock(business_repo, nm_id=100, quantity=5)
        assert await detector.scan() == 0
        bot.reset_mock()

        await upsert_stock(business_repo, nm_id=100, quantity=11)
        created = await detector.scan()

        pending = await stock_arrival_repo.get_pending()
        baselines = await stock_arrival_repo.get_baselines()
        assert created == 1
        assert len(pending) == 1
        assert pending[0]["qty_delta"] == 6
        assert baselines[100]["last_total_full"] == 11
        bot.send_message.assert_awaited_once()
    finally:
        await db.close()


async def test_scan_delta_below_threshold_no_prompt(tmp_path: Path) -> None:
    db, business_repo, stock_arrival_repo, detector, bot = await make_detector(tmp_path)
    try:
        await upsert_stock(business_repo, nm_id=100, quantity=10)
        assert await detector.scan() == 0
        bot.reset_mock()

        await upsert_stock(business_repo, nm_id=100, quantity=14)
        created = await detector.scan()

        assert created == 0
        assert await stock_arrival_repo.count_pending() == 0
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_scan_negative_delta_no_prompt(tmp_path: Path) -> None:
    db, business_repo, stock_arrival_repo, detector, bot = await make_detector(tmp_path)
    try:
        await upsert_stock(business_repo, nm_id=100, quantity=10)
        assert await detector.scan() == 0
        bot.reset_mock()

        await upsert_stock(business_repo, nm_id=100, quantity=4)
        created = await detector.scan()

        baselines = await stock_arrival_repo.get_baselines()
        assert created == 0
        assert await stock_arrival_repo.count_pending() == 0
        assert baselines[100]["last_total_full"] == 4
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_scan_returns_in_flight_no_false_positive(tmp_path: Path) -> None:
    db, business_repo, stock_arrival_repo, detector, bot = await make_detector(tmp_path)
    try:
        await upsert_stock(
            business_repo,
            nm_id=100,
            quantity=5,
            in_way_from_client=2,
        )
        assert await detector.scan() == 0
        bot.reset_mock()

        await upsert_stock(
            business_repo,
            nm_id=100,
            quantity=7,
            in_way_from_client=0,
        )
        created = await detector.scan()

        assert created == 0
        assert await stock_arrival_repo.count_pending() == 0
        bot.send_message.assert_not_awaited()
    finally:
        await db.close()


async def test_scan_multiple_arrivals_creates_multiple_prompts_per_run(tmp_path: Path) -> None:
    db, business_repo, stock_arrival_repo, detector, bot = await make_detector(tmp_path)
    try:
        await upsert_stock(business_repo, nm_id=100, quantity=5)
        await upsert_stock(business_repo, nm_id=101, quantity=10)
        assert await detector.scan() == 0
        bot.reset_mock()

        await upsert_stock(business_repo, nm_id=100, quantity=12)
        await upsert_stock(business_repo, nm_id=101, quantity=16)
        created = await detector.scan()

        pending = await stock_arrival_repo.get_pending()
        assert created == 2
        assert len(pending) == 2
        assert {prompt["nm_id"] for prompt in pending} == {100, 101}
        assert bot.send_message.await_count == 2
    finally:
        await db.close()
