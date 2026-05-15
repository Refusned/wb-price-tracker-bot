from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.storage.db import Database
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository


pytestmark = pytest.mark.asyncio


async def make_repo(tmp_path: Path) -> tuple[Database, DecisionSnapshotRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, DecisionSnapshotRepository(db)


def iso(seconds_offset: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds_offset)).isoformat()


async def test_record_basic(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        inserted_id = await repo.record(
            nm_id=100,
            observed_price=900.0,
            observed_margin_estimate=12.5,
            alert_sent=True,
        )

        assert inserted_id > 0
        rows = await db.fetchall("SELECT * FROM decision_snapshots WHERE id = ?", (inserted_id,))
        assert len(rows) == 1
        assert int(rows[0]["nm_id"]) == 100
        assert float(rows[0]["observed_price"]) == 900.0
        assert float(rows[0]["observed_margin_estimate"]) == 12.5
    finally:
        await db.close()


async def test_record_validates_nm_id_positive(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        with pytest.raises(ValueError):
            await repo.record(
                nm_id=0,
                observed_price=900.0,
                observed_margin_estimate=12.5,
                alert_sent=True,
            )
    finally:
        await db.close()


async def test_record_validates_price_nonneg(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        with pytest.raises(ValueError):
            await repo.record(
                nm_id=100,
                observed_price=-1.0,
                observed_margin_estimate=12.5,
                alert_sent=True,
            )
    finally:
        await db.close()


async def test_alert_sent_bool_to_int(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        inserted_id = await repo.record(
            nm_id=100,
            observed_price=900.0,
            observed_margin_estimate=12.5,
            alert_sent=True,
        )

        row = await db.fetchone("SELECT alert_sent FROM decision_snapshots WHERE id = ?", (inserted_id,))
        assert row is not None
        assert int(row["alert_sent"]) == 1
    finally:
        await db.close()


async def test_update_user_action(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        inserted_id = await repo.record(
            nm_id=100,
            observed_price=900.0,
            observed_margin_estimate=12.5,
            alert_sent=True,
        )

        await repo.update_user_action(inserted_id, "bought")

        row = await db.fetchone("SELECT user_action FROM decision_snapshots WHERE id = ?", (inserted_id,))
        assert row is not None
        assert row["user_action"] == "bought"
    finally:
        await db.close()


async def test_find_recent_for_nm_window(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        await repo.record(
            nm_id=100,
            observed_price=950.0,
            observed_margin_estimate=10.0,
            alert_sent=True,
            snapshot_at=iso(-7200),
        )
        recent_id = await repo.record(
            nm_id=100,
            observed_price=900.0,
            observed_margin_estimate=12.5,
            alert_sent=True,
            snapshot_at=iso(),
        )
        await repo.record(
            nm_id=101,
            observed_price=800.0,
            observed_margin_estimate=14.0,
            alert_sent=True,
            snapshot_at=iso(),
        )

        rows = await repo.find_recent_for_nm(100, within_seconds=3600)

        assert len(rows) == 1
        assert rows[0]["id"] == recent_id
    finally:
        await db.close()


async def test_distribution_basic(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        ids = []
        for idx in range(5):
            ids.append(
                await repo.record(
                    nm_id=100 + (idx % 2),
                    observed_price=900.0 + idx,
                    observed_margin_estimate=12.5,
                    alert_sent=idx < 3,
                    snapshot_at=iso(),
                )
            )

        await repo.update_user_action(ids[0], "bought")
        await repo.update_user_action(ids[1], "ignored")
        await repo.update_user_action(ids[2], "too_late")

        distribution = await repo.distribution(days=30)

        assert distribution["total"] == 5
        assert distribution["alerted"] == 3
        assert distribution["by_action"]["bought"] == 1
        assert distribution["by_action"]["ignored"] == 1
        assert distribution["by_action"]["too_late"] == 1
        assert distribution["by_action"][None] == 2
        assert distribution["by_nm_id_top10"][0]["count"] == 3
    finally:
        await db.close()


async def test_recent_limits(tmp_path: Path) -> None:
    db, repo = await make_repo(tmp_path)
    try:
        for idx in range(25):
            await repo.record(
                nm_id=100 + idx,
                observed_price=900.0 + idx,
                observed_margin_estimate=12.5,
                alert_sent=True,
                snapshot_at=iso(idx),
            )

        rows = await repo.recent(limit=10)

        assert len(rows) == 10
    finally:
        await db.close()
