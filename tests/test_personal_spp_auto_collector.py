from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.personal_spp_auto_collector import (
    META_KEY,
    SOURCE,
    PersonalSppAutoCollector,
)
from app.storage.business_repository import BusinessRepository
from app.storage.db import Database
from app.storage.personal_spp_repository import PersonalSppRepository
from app.storage.repositories import MetaRepository


pytestmark = pytest.mark.asyncio


async def _make_db(tmp_path: Path) -> Database:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


async def _insert_sale(db: Database, *, srid: str, date: str, nm_id: int,
                       category: str, spp: float, is_return: int = 0) -> None:
    await db.execute(
        """INSERT INTO own_sales(srid,g_number,date,last_change_date,nm_id,
                supplier_article,brand,category,warehouse_name,total_price,for_pay,
                price_with_disc,spp_percent,commission_percent,discount_percent,
                is_return,order_type,first_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (srid, f"G-{srid}", date, date, nm_id, "X", "B", category, "WH",
         100.0, 80.0, 100.0, spp, 15.0, 0.0, is_return, "S", date + "T10:00:00Z"),
    )


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _make_collector(db: Database, **kwargs) -> PersonalSppAutoCollector:
    return PersonalSppAutoCollector(
        personal_spp_repo=PersonalSppRepository(db),
        business_repository=BusinessRepository(db),
        meta_repository=MetaRepository(db),
        **kwargs,
    )


async def test_writes_snapshot_per_category(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    today_str = _today()
    for i in range(5):
        await _insert_sale(db, srid=f"S{i}", date=today_str, nm_id=100,
                           category="Cat A", spp=20.0 + i)
    for i in range(4):
        await _insert_sale(db, srid=f"T{i}", date=today_str, nm_id=200,
                           category="Cat B", spp=15.0)
    for i in range(2):
        await _insert_sale(db, srid=f"U{i}", date=today_str, nm_id=300,
                           category="Cat C", spp=18.0)

    c = await _make_collector(db, min_sales_threshold=3)
    written = await c.maybe_collect()
    assert written == 2  # Cat A + Cat B, Cat C below threshold

    history = await PersonalSppRepository(db).history(days=1)
    by_cat = {h["category"]: h for h in history}
    assert "Cat A" in by_cat
    assert "Cat B" in by_cat
    assert "Cat C" not in by_cat
    assert by_cat["Cat A"]["source"] == SOURCE
    # AVG of 20,21,22,23,24 = 22
    assert abs(by_cat["Cat A"]["spp_percent"] - 22.0) < 0.01
    assert abs(by_cat["Cat B"]["spp_percent"] - 15.0) < 0.01
    await db.close()


async def test_idempotent_within_day(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    for i in range(5):
        await _insert_sale(db, srid=f"S{i}", date=_today(), nm_id=100,
                           category="X", spp=24.0)

    c = await _make_collector(db)
    n1 = await c.maybe_collect()
    n2 = await c.maybe_collect()
    n3 = await c.maybe_collect()
    assert n1 == 1
    assert n2 == 0
    assert n3 == 0
    await db.close()


async def test_force_overrides_daily_check(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    for i in range(5):
        await _insert_sale(db, srid=f"S{i}", date=_today(), nm_id=100,
                           category="X", spp=24.0)

    c = await _make_collector(db)
    n1 = await c.maybe_collect()
    n2 = await c.maybe_collect(force=True)
    assert n1 == 1
    assert n2 == 1
    await db.close()


async def test_excludes_returns(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    today_str = _today()
    for i in range(4):
        await _insert_sale(db, srid=f"R{i}", date=today_str, nm_id=100,
                           category="X", spp=50.0, is_return=1)
    for i in range(3):
        await _insert_sale(db, srid=f"S{i}", date=today_str, nm_id=100,
                           category="X", spp=20.0)

    c = await _make_collector(db)
    written = await c.maybe_collect()
    assert written == 1
    history = await PersonalSppRepository(db).history(days=1)
    assert abs(history[0]["spp_percent"] - 20.0) < 0.01
    await db.close()


async def test_no_sales_marks_today_anyway(tmp_path: Path) -> None:
    db = await _make_db(tmp_path)
    meta = MetaRepository(db)
    c = await _make_collector(db)
    written = await c.maybe_collect()
    assert written == 0
    last = await meta.get_value(META_KEY)
    assert last == _today()
    await db.close()
