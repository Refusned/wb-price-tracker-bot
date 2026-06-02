from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.storage.db import Database
from app.storage.dialog_repository import DialogRepository

pytestmark = pytest.mark.asyncio


async def _repo(tmp_path: Path) -> tuple[Database, DialogRepository]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db, DialogRepository(db)


async def test_append_and_get_recent_chronological(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        await repo.append(1, "user", "почему упали продажи?")
        await repo.append(1, "assistant", "из-за остатков")
        await repo.append(1, "user", "а что делать?")
        got = await repo.get_recent(1)
        assert got == [
            {"role": "user", "content": "почему упали продажи?"},
            {"role": "assistant", "content": "из-за остатков"},
            {"role": "user", "content": "а что делать?"},
        ]
    finally:
        await db.close()


async def test_get_recent_limit_keeps_newest_tail(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        for i in range(30):
            await repo.append(7, "user", f"msg{i}")
        got = await repo.get_recent(7, limit=10)
        assert len(got) == 10
        # последние 10 в хронологии: msg20..msg29
        assert got[0]["content"] == "msg20"
        assert got[-1]["content"] == "msg29"
    finally:
        await db.close()


async def test_clear_only_target_chat(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        await repo.append(1, "user", "a")
        await repo.append(2, "user", "b")
        await repo.clear(1)
        assert await repo.count(1) == 0
        assert await repo.count(2) == 1
    finally:
        await db.close()


async def test_invalid_role_rejected(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        with pytest.raises(ValueError):
            await repo.append(1, "tool", "x")
        with pytest.raises(ValueError):
            await repo.append(1, "system", "x")
    finally:
        await db.close()


async def test_concurrent_appends_unique_ids(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        ids = await asyncio.gather(*[repo.append(1, "user", f"m{i}") for i in range(25)])
        assert len(set(ids)) == 25  # execute_insert атомарен → rowid'ы уникальны
        assert await repo.count(1) == 25
    finally:
        await db.close()
