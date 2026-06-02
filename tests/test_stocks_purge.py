"""Безопасность purge в upsert_stocks: полный снимок чистит устаревшее,
частичный (один источник упал) — нет. Только локальная SQLite, без сети.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from app.storage.business_repository import BusinessRepository
from app.storage.db import Database
from app.wb.seller_client import StockEntry

pytestmark = pytest.mark.asyncio

_T0 = "2026-06-01T00:00:00+00:00"
_T1 = "2026-06-01T01:00:00+00:00"


async def _db(tmp_path: Path) -> Database:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


def _entries(nm_ids: Iterable[int]) -> list[StockEntry]:
    return [
        StockEntry(
            nm_id=i, supplier_article=f"A-{i}", warehouse_name="Коледино",
            quantity=10, in_way_to_client=0, in_way_from_client=0,
            quantity_full=10, subject="Товар", last_change_date=_T0,
        )
        for i in nm_ids
    ]


async def _nm_ids(db: Database) -> set[int]:
    rows = await db.fetchall("SELECT nm_id FROM own_stocks")
    return {int(r["nm_id"]) for r in rows}


async def test_purge_stale_true_deletes_missing(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    try:
        repo = BusinessRepository(db)
        await repo.upsert_stocks(_entries([1, 2, 3, 4, 5]), _T0, purge_stale=True)
        # Полный снимок без nm_id=5 (и ≥5 строк) → устаревший nm_id=5 удаляется.
        await repo.upsert_stocks(_entries([1, 2, 3, 4, 6, 7]), _T1, purge_stale=True)
        assert await _nm_ids(db) == {1, 2, 3, 4, 6, 7}
    finally:
        await db.close()


async def test_purge_stale_false_keeps_missing(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    try:
        repo = BusinessRepository(db)
        await repo.upsert_stocks(_entries([1, 2, 3, 4, 5]), _T0, purge_stale=True)
        # Частичный снимок (один источник упал): nm_id=5 отсутствует, но НЕ удаляется.
        await repo.upsert_stocks(_entries([1, 2, 3, 4, 6, 7]), _T1, purge_stale=False)
        assert 5 in await _nm_ids(db)
    finally:
        await db.close()


async def test_purge_below_5_no_delete_even_if_true(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    try:
        repo = BusinessRepository(db)
        await repo.upsert_stocks(_entries([1, 2, 3, 4, 5]), _T0, purge_stale=True)
        # Маленький снимок (<5 строк) не пуржит даже при purge_stale=True (safety guard).
        await repo.upsert_stocks(_entries([1, 2]), _T1, purge_stale=True)
        assert 5 in await _nm_ids(db)
    finally:
        await db.close()
