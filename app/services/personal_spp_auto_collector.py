"""Auto-collector для personal_spp_snapshots из own_sales.

WB Seller API не отдаёт ТВОЮ личную СПП как покупателя. Лучшая прокси:
медианная СПП, которую WB давал buyers за твои товары в недавних продажах.
Это отражает алгоритмическую СПП-регулятику WB в твоих категориях.

Запускается из scheduler.seller_update_once после upsert_sales.
Идемпотентно по дням: одна запись на (категория, день).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.storage.business_repository import BusinessRepository
from app.storage.personal_spp_repository import PersonalSppRepository
from app.storage.repositories import MetaRepository


SOURCE = "auto_from_sales"
META_KEY = "personal_spp_auto_last_date"


class PersonalSppAutoCollector:
    """Daily auto-collection of personal SPP proxy from own_sales.spp_percent."""

    def __init__(
        self,
        *,
        personal_spp_repo: PersonalSppRepository,
        business_repository: BusinessRepository,
        meta_repository: MetaRepository,
        lookback_days: int = 7,
        min_sales_threshold: int = 3,
        logger: logging.Logger | None = None,
    ) -> None:
        self._personal_spp_repo = personal_spp_repo
        self._business_repository = business_repository
        self._meta_repository = meta_repository
        self._lookback_days = max(int(lookback_days), 1)
        self._min_sales_threshold = max(int(min_sales_threshold), 1)
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    async def maybe_collect(self, *, force: bool = False) -> int:
        """Run aggregation if not already done today. Returns snapshots written.

        Args:
            force: skip the "once per day" check (used by /refresh_spp).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if not force:
            last_date = await self._meta_repository.get_value(META_KEY)
            if last_date == today:
                return 0  # already collected today

        # Aggregate from own_sales: median-ish per category using AVG (SQLite
        # doesn't have native MEDIAN, but AVG of a tight 17-27 range is fine).
        # We use a window of last `lookback_days` and only count rows where
        # category is set AND spp_percent > 0 AND not a return.
        rows = await self._business_repository._db.fetchall(
            f"""
            SELECT
                category,
                AVG(spp_percent) AS avg_spp,
                COUNT(*) AS n_sales,
                MIN(spp_percent) AS min_spp,
                MAX(spp_percent) AS max_spp
            FROM own_sales
            WHERE date >= date('now', '-{self._lookback_days} days')
              AND is_return = 0
              AND spp_percent > 0
              AND category IS NOT NULL
              AND category != ''
            GROUP BY category
            HAVING n_sales >= ?
            ORDER BY n_sales DESC
            """,
            (self._min_sales_threshold,),
        )

        if not rows:
            self._logger.info(
                "personal_spp auto: no categories with enough sales (window=%dd, min=%d)",
                self._lookback_days,
                self._min_sales_threshold,
            )
            # Still mark today as processed so we don't re-query repeatedly.
            await self._meta_repository.set_value(META_KEY, today)
            return 0

        snapshot_at = datetime.now(timezone.utc).isoformat()
        written = 0
        for row in rows:
            category = str(row["category"])
            avg_spp = float(row["avg_spp"])
            n_sales = int(row["n_sales"])
            min_spp = float(row["min_spp"])
            max_spp = float(row["max_spp"])

            await self._personal_spp_repo.log_snapshot(
                spp_percent=avg_spp,
                category=category,
                source=SOURCE,
                snapshot_at=snapshot_at,
            )
            written += 1
            self._logger.info(
                "personal_spp auto: %s avg=%.2f (n=%d range=%.1f-%.1f)",
                category, avg_spp, n_sales, min_spp, max_spp,
            )

        await self._meta_repository.set_value(META_KEY, today)
        return written
