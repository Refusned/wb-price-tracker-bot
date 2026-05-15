from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.db import Database
from app.storage.personal_spp_repository import PersonalSppRepository


pytestmark = pytest.mark.asyncio


async def make_repo(tmp_path: Path) -> tuple[Database, PersonalSppRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, PersonalSppRepository(db)


def iso(days_offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days_offset)).isoformat()


async def test_log_snapshot(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        inserted_id = await repo.log_snapshot(24.0)
        assert inserted_id > 0
    finally:
        await db.close()


async def test_log_validates_range(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        with pytest.raises(ValueError):
            await repo.log_snapshot(-0.1)
        with pytest.raises(ValueError):
            await repo.log_snapshot(100.1)
    finally:
        await db.close()


async def test_latest_returns_most_recent(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        await repo.log_snapshot(20.0, snapshot_at=iso(-1))
        await repo.log_snapshot(25.0, snapshot_at=iso(0))
        latest = await repo.latest()
        assert latest is not None
        assert latest["spp_percent"] == 25.0
    finally:
        await db.close()


async def test_history_orders_desc(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        await repo.log_snapshot(20.0, snapshot_at=iso(-2))
        await repo.log_snapshot(21.0, snapshot_at=iso(-1))
        await repo.log_snapshot(22.0, snapshot_at=iso(0))
        rows = await repo.history(days=30)
        assert [row["spp_percent"] for row in rows] == [22.0, 21.0, 20.0]
    finally:
        await db.close()


async def test_trend_computes_drop_pct(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        base = datetime.now(timezone.utc) - timedelta(days=6)
        for i in range(7):
            await repo.log_snapshot(24.0, snapshot_at=(base + timedelta(days=i)).isoformat())
        await repo.log_snapshot(8.0, snapshot_at=datetime.now(timezone.utc).isoformat())

        trend = await repo.trend(window_days=7)
        assert trend is not None
        assert trend["current"] == 8.0
        assert trend["mean"] > 8.0
        assert trend["drop_pct_vs_window"] > 0
    finally:
        await db.close()


async def test_trend_returns_none_on_empty(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        assert await repo.trend() is None
    finally:
        await db.close()
