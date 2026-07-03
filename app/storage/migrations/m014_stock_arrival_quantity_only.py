"""Migration m014: детектор «новой партии» — переход на quantity-only.

Шаг 0 (2026-06-24) показал: WB убрал статистический endpoint incomes (404),
а фантомные «🆕 Новая партия» раздувались полем quantity_full = quantity +
in_way_to_client + in_way_from_client. На боевых данных продавца quantity=0,
а все 44 «единицы» сидели в пути к клиенту (12) и возвратах в пути (32) —
т.е. сигналом служило движение продаж/возвратов, а не реальные приходы.

Детектор переведён на рост ДОСТУПНОГО остатка (own_stocks.quantity).
Эта миграция готовит чистый переход:

  1) Сбрасывает stock_baselines: старые baseline хранят quantity_full и
     несравнимы с новой семантикой. После сброса детектор на следующем
     скане заново засеет baseline по quantity (baseline отсутствует →
     prompt НЕ шлётся), без всплеска на стыке.
  2) Истекает текущие pending-промпты: это фантомы старой логики, чтобы
     продавец не путался в них после деплоя.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

VERSION = 14
NAME = "stock_arrival_quantity_only"


async def up(conn: Any) -> None:
    now = datetime.now(timezone.utc).isoformat()
    # 1) Чистый ре-сид baseline под quantity-only семантику.
    await conn.execute("DELETE FROM stock_baselines")
    # 2) Истечь фантомные pending-промпты старой логики.
    await conn.execute(
        "UPDATE pending_purchase_prompts "
        "SET status = 'expired', resolved_at = ? "
        "WHERE status = 'pending'",
        (now,),
    )


async def apply(conn: Any) -> None:
    await up(conn)
