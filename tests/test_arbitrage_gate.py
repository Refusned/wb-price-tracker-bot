"""Money-safety гейт тарифов в сканере (AND→OR).

Если ХОТЯ БЫ один тариф (комиссия или логистика) не подтверждён, алерт НЕ
должен уходить (иначе маржа считается по захардкоженному фоллбэку и убыток
может выглядеть прибыльным). Кандидат при этом всё равно записывается.
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

_SUBJECT = 8899


async def _make_db(tmp_path: Path) -> Database:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    return db


def _make_config(monkeypatch, **overrides) -> AppConfig:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    cfg = load_config()
    base = {"arbitrage_cohort_min_size": 1, "arbitrage_min_profit_rub": 0,
            "arbitrage_min_margin_percent": 0.0, "arbitrage_min_pprd_percent": 0.0}
    base.update(overrides)
    return dataclasses.replace(cfg, **base)


def _prod(nm: int, price_rub: int) -> dict:
    return {
        "id": nm, "subjectId": _SUBJECT, "subjectName": "Умная колонка",
        "name": f"Станция Миди чёрная {nm}", "brand": "Yandex",
        "feedbacks": 100, "volume": 1.0,
        "sizes": [{"price": {"product": price_rub * 100, "basic": price_rub * 100}}],
    }


class _FakeWb:
    def __init__(self, products): self._p = products
    async def search_for_arbitrage_raw(self, query, max_pages=None): return list(self._p)


class _TariffsCommissionMissing:
    """Логистика есть, комиссии нет — раньше (AND) алерт всё равно уходил."""
    async def get_commission_fbs(self, subject_id): return None
    async def estimate_logistics_for_volume(self, volume_l): return 500.0


class _TariffsBothPresent:
    async def get_commission_fbs(self, subject_id): return 16.0
    async def estimate_logistics_for_volume(self, volume_l): return 500.0


async def _seed_and_scan(db, config, tariffs):
    repo = ArbitrageRepository(db)
    await repo.add_query("Станция Миди", subject_id=_SUBJECT)
    await repo.record_spp_observation(
        nm_id=999000, subject_id=_SUBJECT, subject_name="Умная колонка",
        public_price_rub=15000, my_buyer_price_rub=11000,
        source="checkout_manual", confidence="high",
    )
    business = AsyncMock()
    business.list_recent_own_nm_ids = AsyncMock(return_value=[])
    subs = AsyncMock()
    subs.list_active_chat_ids = AsyncMock(return_value=[123])
    scanner = ArbitrageScanner(
        config=config, wb_client=_FakeWb([_prod(101, 12000), _prod(102, 12500)]),
        bot=AsyncMock(spec=Bot), arb_repo=repo, tariffs_repo=tariffs,
        spp_resolver=PersonalSppResolver(repo), business_repo=business,
        subscriber_repo=subs,
    )
    return await scanner.scan_once()


async def test_missing_commission_records_candidate_but_no_alert(monkeypatch, tmp_path):
    config = _make_config(monkeypatch)
    db = await _make_db(tmp_path)
    result = await _seed_and_scan(db, config, _TariffsCommissionMissing())
    assert result["candidates"] >= 1   # кандидаты записаны для анализа
    assert result["alerted"] == 0      # но алертов нет — гейт сработал
    await db.close()


async def test_both_tariffs_present_allows_alert(monkeypatch, tmp_path):
    config = _make_config(monkeypatch)
    db = await _make_db(tmp_path)
    result = await _seed_and_scan(db, config, _TariffsBothPresent())
    assert result["candidates"] >= 1
    assert result["alerted"] >= 1      # sanity: гейт не глушит валидные алерты
    await db.close()
