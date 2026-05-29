"""Characterization + keyword-filter tests for ArbitrageScanner.

Этап 1 (Day 19): before wiring the per-query color/variant keyword filter into
the scanner, we pin current ``scan_once()`` behaviour with a characterization
test (no scanner tests existed before — flagged by /autoplan eng review). Then
the keyword-filter tests assert the new behaviour: products whose name does not
match a query's include/exclude keywords are dropped from the cohort BEFORE
price metrics, while queries WITHOUT keywords are unaffected.

The LLM is NOT involved here — this is the deterministic baseline.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot

from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.scanner import ArbitrageScanner
from app.arbitrage.spp_resolver import PersonalSppResolver
from app.config import AppConfig, load_config
from app.storage.db import Database


pytestmark = pytest.mark.asyncio

_SUBJECT = 8899  # Умная колонка


async def _make_db(tmp_path: Path) -> Database:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


def _make_config(monkeypatch, **overrides) -> AppConfig:
    # Avoid reading a real .env; load defaults with only BOT_TOKEN set.
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    cfg = load_config()
    # cohort_min_size=1 lets tests use tiny cohorts.
    base = {"arbitrage_cohort_min_size": 1}
    base.update(overrides)
    return dataclasses.replace(cfg, **base)


def _prod(nm: int, name: str, price_rub: int, *, subject_id: int = _SUBJECT,
          brand: str = "Yandex", feedbacks: int = 100, volume: float = 1.0) -> dict:
    """Raw WB v18-shaped product dict (price in kopecks under sizes[0].price)."""
    return {
        "id": nm,
        "subjectId": subject_id,
        "subjectName": "Умная колонка",
        "name": name,
        "brand": brand,
        "feedbacks": feedbacks,
        "volume": volume,
        "sizes": [{"price": {"product": price_rub * 100, "basic": price_rub * 100}}],
    }


class _FakeWb:
    def __init__(self, products: list[dict]) -> None:
        self._products = products

    async def search_for_arbitrage_raw(self, query: str, max_pages=None) -> list[dict]:
        return list(self._products)


async def _make_scanner(db: Database, config: AppConfig, products: list[dict]) -> ArbitrageScanner:
    repo = ArbitrageRepository(db)
    # Seed one category observation so СПП resolves (category-wide model).
    await repo.record_spp_observation(
        nm_id=999000, subject_id=_SUBJECT, subject_name="Умная колонка",
        public_price_rub=15000, my_buyer_price_rub=11000,
        source="checkout_manual", confidence="high",
    )
    tariffs = AsyncMock()
    tariffs.get_commission_fbs = AsyncMock(return_value=16.0)
    tariffs.estimate_logistics_for_volume = AsyncMock(return_value=182)
    business = AsyncMock()
    business.list_recent_own_nm_ids = AsyncMock(return_value=[])
    subs = AsyncMock()
    subs.list_active_chat_ids = AsyncMock(return_value=[])
    bot = AsyncMock(spec=Bot)
    return ArbitrageScanner(
        config=config,
        wb_client=_FakeWb(products),
        bot=bot,
        arb_repo=repo,
        tariffs_repo=tariffs,
        spp_resolver=PersonalSppResolver(repo),
        business_repo=business,
        subscriber_repo=subs,
    )


async def _recorded_nm_ids(db: Database) -> set[int]:
    rows = await db.fetchall("SELECT nm_id FROM arb_candidates")
    return {int(r["nm_id"]) for r in rows}


# ── 1.2 Characterization baseline (must pass on current + new code) ──────────

async def test_scan_once_baseline_records_all_cohort(tmp_path, monkeypatch) -> None:
    """Without keywords, every valid cohort product becomes a candidate."""
    config = _make_config(monkeypatch)
    products = [
        _prod(101, "Умная колонка Яндекс Станция Миди чёрная", 12000),
        _prod(102, "Умная колонка Яндекс Станция Миди серая", 12500),
        _prod(103, "Умная колонка Яндекс Станция Миди жёлтая", 13000),
    ]
    db = await _make_db(tmp_path)
    repo = ArbitrageRepository(db)
    scanner = await _make_scanner(db, config, products)
    await repo.add_query("Станция Миди", subject_id=_SUBJECT)

    result = await scanner.scan_once()

    assert result["queries"] == 1
    assert result["candidates"] == 3
    assert await _recorded_nm_ids(db) == {101, 102, 103}
    await db.close()


# ── 1.1 Per-query keyword filter ─────────────────────────────────────────────

async def test_keyword_filter_excludes_wrong_color(tmp_path, monkeypatch) -> None:
    """Query with include keywords drops non-matching colors before metrics."""
    config = _make_config(monkeypatch)
    products = [
        _prod(101, "Умная колонка Яндекс Станция Миди чёрная", 12000),
        _prod(102, "Умная колонка Яндекс Станция Миди серая", 12500),
        _prod(103, "Умная колонка Яндекс Станция Миди жёлтая", 13000),
    ]
    db = await _make_db(tmp_path)
    repo = ArbitrageRepository(db)
    scanner = await _make_scanner(db, config, products)
    qid = await repo.add_query("Станция Миди", subject_id=_SUBJECT)
    await repo.set_query_keywords(qid, include="чёрн,серая,серый", exclude="")

    result = await scanner.scan_once()

    # Yellow (103) filtered out; black + grey kept.
    assert await _recorded_nm_ids(db) == {101, 102}
    assert result["candidates"] == 2
    await db.close()


async def test_keyword_filter_exclude_wins(tmp_path, monkeypatch) -> None:
    """Exclude keyword removes a matching item even if include also matches."""
    config = _make_config(monkeypatch)
    products = [
        _prod(101, "Станция Миди чёрная", 12000),
        _prod(102, "Станция Миди чёрная восстановленная", 9000),
        _prod(103, "Станция Миди серая", 12500),
    ]
    db = await _make_db(tmp_path)
    repo = ArbitrageRepository(db)
    scanner = await _make_scanner(db, config, products)
    qid = await repo.add_query("Станция Миди", subject_id=_SUBJECT)
    await repo.set_query_keywords(qid, include="чёрн,серая", exclude="восстановл")

    await scanner.scan_once()

    assert await _recorded_nm_ids(db) == {101, 103}
    await db.close()


async def test_no_keywords_does_not_filter(tmp_path, monkeypatch) -> None:
    """Regression: a query WITHOUT keywords (e.g. robot vacuums) keeps all.

    Guards against the global-whitelist bug: reusing the Станция color set
    across all queries would wrongly empty cohorts that have no color in name.
    """
    config = _make_config(monkeypatch)
    products = [
        _prod(201, "Робot-пылесос Xiaomi S20", 18000, subject_id=2791, brand="Xiaomi"),
        _prod(202, "Робот-пылесос Xiaomi X20 Pro", 22000, subject_id=2791, brand="Xiaomi"),
        _prod(203, "Робот-пылесос Xiaomi E10", 14000, subject_id=2791, brand="Xiaomi"),
    ]
    db = await _make_db(tmp_path)
    repo = ArbitrageRepository(db)
    # Need a category observation for subject 2791 so СПП resolves.
    await repo.record_spp_observation(
        nm_id=999111, subject_id=2791, subject_name="Роботы-пылесосы",
        public_price_rub=20000, my_buyer_price_rub=15000,
        source="checkout_manual", confidence="high",
    )
    scanner = await _make_scanner(db, config, products)
    await repo.add_query("робот пылесос xiaomi", subject_id=2791)  # no keywords

    result = await scanner.scan_once()

    assert await _recorded_nm_ids(db) == {201, 202, 203}
    assert result["candidates"] == 3
    await db.close()
