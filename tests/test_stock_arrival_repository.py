from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.db import Database
from app.storage.stock_arrival_repository import StockArrivalRepository


pytestmark = pytest.mark.asyncio


async def make_repo(tmp_path: Path) -> tuple[Database, StockArrivalRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, StockArrivalRepository(db)


def iso(hours_offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours_offset)).isoformat()


async def test_baselines_empty_then_upsert(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        assert await repo.get_baselines() == {}

        await repo.upsert_baselines(
            [
                {
                    "nm_id": 100,
                    "supplier_article": "A-100",
                    "last_total_full": 7,
                    "last_seen_at": iso(),
                }
            ]
        )

        baselines = await repo.get_baselines()
        assert baselines[100]["supplier_article"] == "A-100"
        assert baselines[100]["last_total_full"] == 7
    finally:
        await db.close()


async def test_baselines_upsert_idempotent(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        await repo.upsert_baselines(
            [
                {
                    "nm_id": 100,
                    "supplier_article": "A-100",
                    "last_total_full": 7,
                    "last_seen_at": iso(-1),
                }
            ]
        )
        await repo.upsert_baselines(
            [
                {
                    "nm_id": 100,
                    "supplier_article": "A-101",
                    "last_total_full": 11,
                    "last_seen_at": iso(),
                }
            ]
        )

        baselines = await repo.get_baselines()
        assert len(baselines) == 1
        assert baselines[100]["supplier_article"] == "A-101"
        assert baselines[100]["last_total_full"] == 11
    finally:
        await db.close()


async def test_create_prompt_returns_id(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        prompt_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=iso(),
            chat_id=123,
        )

        assert prompt_id is not None
        prompt = await repo.get_prompt(prompt_id)
        assert prompt is not None
        assert prompt["status"] == "pending"
        assert prompt["qty_delta"] == 6
    finally:
        await db.close()


async def test_create_prompt_duplicate_returns_none(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        detected_at = iso()
        first_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=detected_at,
            chat_id=123,
        )
        second_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=detected_at,
            chat_id=123,
        )

        assert first_id is not None
        assert second_id is None
        assert await repo.count_pending() == 1
    finally:
        await db.close()


async def test_get_pending_orders_by_recent(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        old_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=iso(-2),
            chat_id=123,
        )
        new_id = await repo.create_prompt(
            nm_id=101,
            supplier_article="A-101",
            qty_delta=8,
            baseline_total=2,
            current_total=10,
            detected_at=iso(-1),
            chat_id=123,
        )

        rows = await repo.get_pending(limit=10)

        assert [row["id"] for row in rows] == [new_id, old_id]
    finally:
        await db.close()


async def test_resolve_status_invalid_raises(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        prompt_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=iso(),
            chat_id=123,
        )
        assert prompt_id is not None

        with pytest.raises(ValueError):
            await repo.resolve(prompt_id, "done")
    finally:
        await db.close()


async def test_resolve_marks_replied_with_purchase_id(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        prompt_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=iso(),
            chat_id=123,
        )
        assert prompt_id is not None

        await repo.resolve(prompt_id, "replied", purchase_id=42)

        prompt = await repo.get_prompt(prompt_id)
        assert prompt is not None
        assert prompt["status"] == "replied"
        assert prompt["purchase_id"] == 42
        assert prompt["resolved_at"] is not None
    finally:
        await db.close()


async def test_expire_old_marks_expired(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        old_id = await repo.create_prompt(
            nm_id=100,
            supplier_article="A-100",
            qty_delta=6,
            baseline_total=5,
            current_total=11,
            detected_at=iso(-100),
            chat_id=123,
        )
        fresh_id = await repo.create_prompt(
            nm_id=101,
            supplier_article="A-101",
            qty_delta=8,
            baseline_total=2,
            current_total=10,
            detected_at=iso(-1),
            chat_id=123,
        )
        assert old_id is not None
        assert fresh_id is not None

        expired = await repo.expire_old(hours=72)

        old_prompt = await repo.get_prompt(old_id)
        fresh_prompt = await repo.get_prompt(fresh_id)
        assert expired == 1
        assert old_prompt is not None
        assert old_prompt["status"] == "expired"
        assert fresh_prompt is not None
        assert fresh_prompt["status"] == "pending"
    finally:
        await db.close()
