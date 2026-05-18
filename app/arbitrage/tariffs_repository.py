"""CRUD for arb_tariffs_commission (versioned) and arb_tariffs_box."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.storage.db import Database


class TariffsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ── Commission ─────────────────────────────────────────────
    async def upsert_commission(self, rows: list[dict[str, Any]]) -> int:
        """Upsert commission rates. Versioned by effective_from.

        Each row expects keys: subjectID, subjectName, parentID, parentName,
        kgvpMarketplace, kgvpSupplier, kgvpBooking, kgvpPickup, paidStorageKgvp.
        """
        now = datetime.now(timezone.utc).isoformat()
        # Используем 'now' как effective_from для refresh batch — это approximation.
        # Точная дата изменения тарифа неизвестна из API без отдельной отметки.
        effective_from = now[:10]  # YYYY-MM-DD

        async def _tx(conn) -> None:
            for r in rows:
                subj_id = r.get("subjectID")
                if subj_id is None:
                    continue
                await conn.execute(
                    """
                    INSERT INTO arb_tariffs_commission (
                        subject_id, subject_name, parent_id, parent_name,
                        kgvp_marketplace, kgvp_supplier, kgvp_booking, kgvp_pickup,
                        paid_storage_kgvp, effective_from, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subject_id, effective_from) DO UPDATE SET
                        subject_name = excluded.subject_name,
                        parent_id = excluded.parent_id,
                        parent_name = excluded.parent_name,
                        kgvp_marketplace = excluded.kgvp_marketplace,
                        kgvp_supplier = excluded.kgvp_supplier,
                        kgvp_booking = excluded.kgvp_booking,
                        kgvp_pickup = excluded.kgvp_pickup,
                        paid_storage_kgvp = excluded.paid_storage_kgvp,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        int(subj_id),
                        r.get("subjectName"),
                        r.get("parentID"),
                        r.get("parentName"),
                        _to_float(r.get("kgvpMarketplace")),
                        _to_float(r.get("kgvpSupplier")),
                        _to_float(r.get("kgvpBooking")),
                        _to_float(r.get("kgvpPickup")),
                        _to_float(r.get("paidStorageKgvp")),
                        effective_from,
                        now,
                    ),
                )

        await self._db.transaction(_tx)
        return len(rows)

    async def get_commission_fbs(self, subject_id: int) -> float | None:
        """Returns latest effective `kgvpMarketplace` % for the subject."""
        row = await self._db.fetchone(
            """
            SELECT kgvp_marketplace
            FROM arb_tariffs_commission
            WHERE subject_id = ?
            ORDER BY effective_from DESC
            LIMIT 1
            """,
            (int(subject_id),),
        )
        if not row or row["kgvp_marketplace"] is None:
            return None
        return float(row["kgvp_marketplace"])

    async def commission_count(self) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS c FROM arb_tariffs_commission")
        return int(row["c"] or 0) if row else 0

    # ── Box (logistics) ────────────────────────────────────────
    async def upsert_box(self, warehouses: list[dict[str, Any]]) -> int:
        """Upsert FBS box tariffs. effective_date is today (snapshot).

        Each item expects: warehouseName, geoName, boxDeliveryBase, boxDeliveryLiter,
        boxDeliveryMarketplaceBase, boxDeliveryMarketplaceLiter, boxStorageBase, boxStorageLiter.
        Values may come as strings with comma decimal — we parse defensively.
        """
        now = datetime.now(timezone.utc).isoformat()
        effective_date = now[:10]

        async def _tx(conn) -> None:
            for w in warehouses:
                wh_name = w.get("warehouseName") or ""
                if not wh_name:
                    continue
                geo = w.get("geoName") or ""
                await conn.execute(
                    """
                    INSERT INTO arb_tariffs_box (
                        warehouse_name, geo_name,
                        box_delivery_base, box_delivery_liter,
                        box_delivery_marketplace_base, box_delivery_marketplace_liter,
                        box_storage_base, box_storage_liter,
                        effective_date, fetched_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(warehouse_name, geo_name, effective_date) DO UPDATE SET
                        box_delivery_base = excluded.box_delivery_base,
                        box_delivery_liter = excluded.box_delivery_liter,
                        box_delivery_marketplace_base = excluded.box_delivery_marketplace_base,
                        box_delivery_marketplace_liter = excluded.box_delivery_marketplace_liter,
                        box_storage_base = excluded.box_storage_base,
                        box_storage_liter = excluded.box_storage_liter,
                        fetched_at = excluded.fetched_at
                    """,
                    (
                        wh_name, geo,
                        _to_float(w.get("boxDeliveryBase")),
                        _to_float(w.get("boxDeliveryLiter")),
                        _to_float(w.get("boxDeliveryMarketplaceBase")),
                        _to_float(w.get("boxDeliveryMarketplaceLiter")),
                        _to_float(w.get("boxStorageBase")),
                        _to_float(w.get("boxStorageLiter")),
                        effective_date, now,
                    ),
                )

        await self._db.transaction(_tx)
        return len(warehouses)

    async def estimate_logistics_for_volume(
        self,
        volume_l: float,
        *,
        warehouse_preference: str | None = None,
    ) -> float | None:
        """Estimate FBS delivery cost for an item of given volume (liters).

        Uses ``boxDeliveryMarketplaceBase + boxDeliveryMarketplaceLiter × max(volume-1, 0)``.
        Picks warehouse_preference if provided and present, otherwise median across all.
        """
        rows = await self._db.fetchall(
            """
            SELECT warehouse_name,
                   box_delivery_marketplace_base AS base,
                   box_delivery_marketplace_liter AS liter
            FROM arb_tariffs_box
            WHERE effective_date = (SELECT MAX(effective_date) FROM arb_tariffs_box)
              AND box_delivery_marketplace_base IS NOT NULL
              AND box_delivery_marketplace_liter IS NOT NULL
            """
        )
        if not rows:
            return None

        candidates = []
        for r in rows:
            base, liter = r["base"], r["liter"]
            if base is None or liter is None:
                continue
            est = float(base) + float(liter) * max(volume_l - 1.0, 0.0)
            candidates.append((str(r["warehouse_name"]), est))

        if warehouse_preference:
            for name, est in candidates:
                if warehouse_preference.lower() in name.lower():
                    return est
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1])
        return candidates[len(candidates) // 2][1]  # median


def _to_float(value: Any) -> float | None:
    """Parse WB-style numbers: strings like '46', '14,5', or numerics."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".").replace(" ", "")
        if not cleaned or cleaned == "-":
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None
