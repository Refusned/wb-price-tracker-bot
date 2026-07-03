"""Репро + фикс бага AutoObserver: composite-СПП против цены ПОСЛЕ СПП.

Граунд-трус (коммит cc19124 + tools/spp_probe.py 2026-06-10, арт 876392996):
card.wb.ru v4 ``sizes[].price.product`` — цена ПОСЛЕ WB-Скидки (СПП): при
listed 15000₽ API отдаёт ~11250₽; ``basic`` — фейк-РРЦ (~1.8× к listed).
AutoObserver считал наблюдение против product и получал ~кошелёк (5%) вместо
композита (~29%) → decompose_composite_spp давал cat_spp≈0, нули разбавляли
category_avg — сканер системно занижал margin_rub и пропускал связки.

Фикс: для СВОИХ артикулов listed берётся из Statistics API продаж
(own_sales.price_with_disc, медиана за 30д) → наблюдение композитное; для
чужих listed недоступна → наблюдение помечается wallet_only=1 и НЕ участвует
ни в категорийной, ни в per-nm СПП (миграция m013 бэкфиллит историю).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from app.arbitrage.auto_observer import AutoObserver
from app.arbitrage.margin import decompose_composite_spp
from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.spp_resolver import PersonalSppResolver
from app.storage.business_repository import BusinessRepository
from app.storage.db import Database
from app.storage.models import Item


pytestmark = pytest.mark.asyncio

_SUBJECT = 8899          # Умные колонки
_NM_OWN = 876392996      # свой артикул (граунд-трус cc19124)
_NM_ALIEN = 555001       # чужой артикул (продаж в own_sales нет)
_LISTED = 15000          # цена продавца ДО СПП (кабинет / Statistics API)
_CARD_PRODUCT = 11250    # card v4 price.product = 15000 × (1 − СПП 25%)
_PAID = 10658            # чекаут: 11250 × (1 − кошелёк ~5.3%)


class _FakeWb:
    """card.wb.ru v4: price_rub = sizes[].price.product = цена ПОСЛЕ СПП."""

    def __init__(self, *, product_price_rub: int = _CARD_PRODUCT) -> None:
        self._price = product_price_rub

    async def fetch_cards_batch(self, nm_ids: list[str], **kwargs) -> list[Item]:
        nm = str(nm_ids[0])
        return [Item(
            nm_id=nm, name="Умная колонка Станция Миди",
            price_rub=float(self._price), old_price_rub=None,
            in_stock=True, stock_qty=5,
            url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
        )]

    async def search_for_arbitrage_raw(self, query: str, max_pages=None) -> list[dict]:
        return [{"id": int(query), "subjectId": _SUBJECT,
                 "subjectName": "Умные колонки"}]


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    """БД с закрытием в teardown — иначе упавший assert оставляет живую
    aiosqlite-нить и pytest зависает на выходе."""
    database = Database((tmp_path / "app.db").as_posix())
    await database.connect()
    await database.migrate()
    await database.apply_migrations()
    yield database
    await database.close()


async def _seed_own_sales(db: Database, *, nm_id: int, listed: float,
                          n: int = 3) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for i in range(n):
        await db.execute(
            """INSERT INTO own_sales(srid,g_number,date,last_change_date,nm_id,
                    supplier_article,brand,category,warehouse_name,total_price,for_pay,
                    price_with_disc,spp_percent,commission_percent,discount_percent,
                    is_return,order_type,first_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"AO{nm_id}-{i}", f"G-{nm_id}-{i}", today, today, nm_id, "X", "B",
             "Умные колонки", "WH", listed * 2, listed * 0.78, listed, 25.0,
             15.0, 50.0, 0, "S", today + "T10:00:00Z"),
        )


def _make_observer(db: Database) -> AutoObserver:
    return AutoObserver(
        wb_client=_FakeWb(),
        arb_repo=ArbitrageRepository(db),
        business_repo=BusinessRepository(db),
    )


async def test_own_article_composite_spp_from_stats_listed(db: Database) -> None:
    """Репро бага: свой артикул с известным listed → cat_spp должен быть ~24%.

    До фикса public бралась из card API (11250, уже ПОСЛЕ СПП): наблюдение
    выходило ~5.3% (только кошелёк) и decompose давал cat_spp≈0. После фикса
    public = listed из own_sales → композит ~28.9% → cat_spp≈24.4%
    (граунд-трус «СПП колонок 21-25%»).
    """
    await _seed_own_sales(db, nm_id=_NM_OWN, listed=_LISTED)

    obs = await _make_observer(db).observe(
        nm_id=_NM_OWN, paid_price_rub=_PAID, source="purchase", note="test",
    )

    assert obs.ok
    assert obs.wallet_only is False
    assert obs.public_price_rub == _LISTED      # БЫЛО: 11250 (цена после СПП)
    cat_spp = decompose_composite_spp(obs.spp_percent, wallet_pct=6.0)
    assert 22.0 < cat_spp < 27.0                # БЫЛО: ≈0

    # Композитное наблюдение участвует в категорийной СПП
    repo = ArbitrageRepository(db)
    cat = await repo.get_category_avg_spp(_SUBJECT, days=30, min_samples=1)
    assert cat is not None
    assert 27.0 < cat["avg_spp"] < 31.0


async def test_alien_article_is_wallet_only_and_quarantined(db: Database) -> None:
    """Чужой артикул: listed из публичного API не узнать (basic = фейк-РРЦ).

    Наблюдение пишется для аудита/калибровки кошелька, но помечается
    wallet_only и НЕ идёт ни в category_avg, ни в per-nm: резолвер обязан
    вернуть None («нет данных»), а не разбавленный почти-ноль.
    """
    repo = ArbitrageRepository(db)

    obs = await _make_observer(db).observe(
        nm_id=_NM_ALIEN, paid_price_rub=_PAID, source="purchase", note="test",
    )

    assert obs.ok
    assert obs.wallet_only is True
    assert obs.public_price_rub == _CARD_PRODUCT
    row = await db.fetchone(
        "SELECT wallet_only FROM arb_buyer_spp_observations WHERE nm_id = ?",
        (_NM_ALIEN,),
    )
    assert row is not None and int(row["wallet_only"]) == 1

    assert await repo.get_category_avg_spp(_SUBJECT, days=30, min_samples=1) is None
    resolver = PersonalSppResolver(repo)
    assert await resolver.resolve(nm_id=_NM_ALIEN, subject_id=_SUBJECT) is None


async def test_wallet_only_does_not_dilute_manual_observations(db: Database) -> None:
    """Ручное /arb_observe (28.95%) + авто-наблюдение чужого (~5.3%):
    category_avg остаётся ~28.95%, а не «среднее» ~17%."""
    repo = ArbitrageRepository(db)
    await repo.record_spp_observation(
        nm_id=999000, subject_id=_SUBJECT, subject_name="Умные колонки",
        public_price_rub=_LISTED, my_buyer_price_rub=_PAID,
        source="checkout_manual", confidence="high", note="manual /arb_observe",
    )
    await _make_observer(db).observe(
        nm_id=_NM_ALIEN, paid_price_rub=_PAID,
        source="checkout_manual", note="quickadd",
    )

    cat = await repo.get_category_avg_spp(_SUBJECT, days=30, min_samples=1)
    assert cat is not None
    assert abs(cat["avg_spp"] - 28.95) < 0.1
    assert cat["samples"] == 1


async def test_m013_backfills_legacy_auto_observations(db: Database) -> None:
    """Бэкфилл m013: исторические авто-строки (source='purchase' и
    note IN ('quickadd','bulk')) помечаются wallet_only=1, ручные — нет."""
    # Эмулируем БД до m013: колонка отсутствует, миграция «не применена»
    await db.execute("ALTER TABLE arb_buyer_spp_observations DROP COLUMN wallet_only")
    await db.execute("DELETE FROM schema_migrations WHERE version = 13")
    now = datetime.now(timezone.utc).isoformat()
    legacy = [
        (1, "purchase", "auto from /buy purchase #5"),
        (2, "checkout_manual", "quickadd"),
        (3, "checkout_manual", "bulk"),
        (4, "checkout_manual", "manual /arb_observe"),
    ]
    for nm, source, note in legacy:
        await db.execute(
            """INSERT INTO arb_buyer_spp_observations
               (nm_id, subject_id, subject_name, public_price_rub,
                my_buyer_price_rub, spp_percent_observed, source, confidence,
                sample_count, observed_at, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (nm, _SUBJECT, "Умные колонки", _CARD_PRODUCT, _PAID, 5.26,
             source, "high", now, note),
        )

    applied = await db.apply_migrations()

    assert 13 in applied
    rows = await db.fetchall(
        "SELECT nm_id, wallet_only FROM arb_buyer_spp_observations ORDER BY nm_id"
    )
    flags = {int(r["nm_id"]): int(r["wallet_only"]) for r in rows}
    assert flags == {1: 1, 2: 1, 3: 1, 4: 0}
