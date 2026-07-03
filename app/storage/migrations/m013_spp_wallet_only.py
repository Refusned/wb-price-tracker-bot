"""Migration m013: карантин wallet-only СПП-наблюдений (авто-наблюдения).

Граунд-трус (коммит cc19124 + tools/spp_probe.py 2026-06-10, арт 876392996):
публичная цена card.wb.ru v4 ``sizes[].price.product`` — это цена ПОСЛЕ
WB-Скидки (СПП), а ``basic`` — фейк-РРЦ (~1.8× к listed продавца). Поэтому
авто-наблюдения (AutoObserver: хук /buy, stock-arrival prompt, /arb_quickadd,
/arb_bulk), считавшие СПП против product, фиксировали только бонус кошелька
(~5-6%) вместо композита (~29%) — и разбавляли category_avg почти-нулями,
из-за чего сканер арбитража системно занижал margin_rub и пропускал связки.

Колонка wallet_only: 1 = наблюдение измеряет только бонус кошелька и НЕ
участвует в категорийной/per-nm СПП (фильтры в ArbitrageRepository).

Бэкфилл помечает все исторические авто-строки: source='purchase' (хуки /buy
и stock-arrival prompt) и note IN ('quickadd','bulk') (/arb_quickadd,
/arb_bulk). Ручные /arb_observe (note='manual /arb_observe') остаются
композитными — там public вводится руками как listed.
"""
from __future__ import annotations

from typing import Any

VERSION = 13
NAME = "spp_wallet_only"


async def up(conn: Any) -> None:
    # ALTER TABLE ADD COLUMN is not idempotent under SQLite, so check first.
    cursor = await conn.execute("PRAGMA table_info(arb_buyer_spp_observations)")
    columns = await cursor.fetchall()
    await cursor.close()
    column_names = {row[1] for row in columns}

    if "wallet_only" not in column_names:
        await conn.execute(
            "ALTER TABLE arb_buyer_spp_observations "
            "ADD COLUMN wallet_only INTEGER NOT NULL DEFAULT 0"
        )
        await conn.execute(
            "UPDATE arb_buyer_spp_observations SET wallet_only = 1 "
            "WHERE source = 'purchase' OR note IN ('quickadd', 'bulk')"
        )


async def apply(conn: Any) -> None:
    await up(conn)
