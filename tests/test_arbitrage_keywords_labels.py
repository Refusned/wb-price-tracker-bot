"""Repo tests for Этап 1: per-query keyword filter + ground-truth labels."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.arbitrage.repository import ArbitrageRepository
from app.storage.db import Database


pytestmark = pytest.mark.asyncio


async def _make_repo(tmp_path: Path) -> tuple[Database, ArbitrageRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, ArbitrageRepository(db)


async def test_set_and_get_query_keywords(tmp_path) -> None:
    db, repo = await _make_repo(tmp_path)
    qid = await repo.add_query("Станция Миди", subject_id=8899)

    # Default: no keywords.
    q = (await repo.list_queries(only_enabled=False))[0]
    assert q["include_keywords"] is None
    assert q["exclude_keywords"] is None

    await repo.set_query_keywords(qid, include="чёрн,серая", exclude="восстановл")
    q = (await repo.list_queries(only_enabled=False))[0]
    assert q["include_keywords"] == "чёрн,серая"
    assert q["exclude_keywords"] == "восстановл"
    await db.close()


async def test_empty_keywords_stored_as_null(tmp_path) -> None:
    db, repo = await _make_repo(tmp_path)
    qid = await repo.add_query("Станция Миди")
    await repo.set_query_keywords(qid, include="чёрн", exclude="x")
    # Clearing with empty strings → NULL (treated as "no filter").
    await repo.set_query_keywords(qid, include="", exclude="   ")
    q = (await repo.list_queries(only_enabled=False))[0]
    assert q["include_keywords"] is None
    assert q["exclude_keywords"] is None
    await db.close()


async def test_add_nm_label_and_counts(tmp_path) -> None:
    db, repo = await _make_repo(tmp_path)
    await repo.add_nm_label(304333036, "wrong_color", note="жёлтая")
    await repo.add_nm_label(216880682, "bought")
    await repo.add_nm_label(111, "wrong_color")

    counts = await repo.label_counts(days=30)
    assert counts["wrong_color"] == 2
    assert counts["bought"] == 1
    await db.close()


async def test_invalid_label_rejected(tmp_path) -> None:
    db, repo = await _make_repo(tmp_path)
    with pytest.raises(ValueError):
        await repo.add_nm_label(123, "not_a_real_label")
    await db.close()
