"""Auto-fetch buyer-side personal СПП observations.

Three ways to generate ``arb_buyer_spp_observations`` automatically:

1. From a /buy or /addpurchase event — when owner records a real purchase
   we fetch public WB price for that nm_id and store residual СПП.
2. /arb_quickadd <nm> <my_price> — fetch public + compute + save in one shot.
3. /arb_bulk — multiline paste of "nm price" pairs.

Семантика цен (верифицировано коммитом cc19124 + tools/spp_probe.py
2026-06-10, арт 876392996): card.wb.ru v4 ``sizes[].price.product`` — цена
ПОСЛЕ WB-Скидки (СПП), а ``basic`` — фейк-РРЦ (~1.8× к listed). Значит
против card-цены композитную СПП посчитать НЕЛЬЗЯ — получается только
бонус кошелька (~5-6%).

Отсюда два режима наблюдений:

- СВОЙ артикул: listed (цена ДО СПП) берём из Statistics API продаж
  (own_sales.price_with_disc, медиана за 30д) → наблюдение КОМПОЗИТНОЕ
  (cat_СПП + кошелёк), участвует в category_avg.
- ЧУЖОЙ артикул: listed из публичного API недоступна → наблюдение
  wallet-only (только кошелёк), пишется для аудита/калибровки, но НЕ
  подмешивается в категорийную/per-nm СПП (см. m013).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.arbitrage.repository import ArbitrageRepository
from app.storage.business_repository import BusinessRepository
from app.wb.client import WildberriesClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ObserveResult:
    nm_id: int
    public_price_rub: int
    paid_price_rub: int
    spp_percent: float
    observation_id: int | None
    skipped_reason: str | None = None
    # True — public взята из card API (после СПП): наблюдение измеряет только
    # бонус кошелька и не участвует в категорийной СПП.
    wallet_only: bool = False

    @property
    def ok(self) -> bool:
        return self.observation_id is not None


class AutoObserver:
    """Fetches public WB price and records an SPP observation."""

    def __init__(
        self,
        *,
        wb_client: WildberriesClient,
        arb_repo: ArbitrageRepository,
        business_repo: BusinessRepository | None = None,
    ) -> None:
        self._wb = wb_client
        self._repo = arb_repo
        # Без business_repo listed своих артикулов недоступен — все
        # наблюдения консервативно помечаются wallet-only.
        self._business = business_repo

    async def observe(
        self,
        *,
        nm_id: int,
        paid_price_rub: int,
        source: str = "purchase",
        note: str | None = None,
    ) -> ObserveResult:
        """Resolve base price for nm_id, compute СПП, record observation.

        Returns an ObserveResult with details (or skipped_reason on error).
        Never raises — failures are reported via skipped_reason.
        """
        if not isinstance(nm_id, int) or nm_id <= 0:
            return ObserveResult(
                nm_id=int(nm_id) if isinstance(nm_id, int) else 0,
                public_price_rub=0, paid_price_rub=int(paid_price_rub),
                spp_percent=0.0, observation_id=None,
                skipped_reason="invalid_nm_id",
            )

        # Свой артикул? Тогда listed (цена ДО СПП) известна из Statistics API
        # продаж — наблюдение композитное, card API не нужен (и работает даже
        # для out-of-stock карточки). Для чужих артикулов listed недоступна:
        # card v4 product = цена ПОСЛЕ СПП, basic = фейк-РРЦ — наблюдение
        # помечается wallet-only.
        own_listed: float | None = None
        if self._business is not None:
            try:
                own_listed = await self._business.median_listed_price_for_nm(nm_id)
            except Exception:
                logger.exception("AutoObserver: own-listed lookup failed for nm=%s", nm_id)
                own_listed = None

        wallet_only = own_listed is None
        if own_listed is not None:
            public_price = int(round(own_listed))
        else:
            try:
                items = await self._wb.fetch_cards_batch([str(nm_id)])
            except Exception:
                logger.exception("AutoObserver: WB fetch_cards_batch failed for nm=%s", nm_id)
                return ObserveResult(
                    nm_id=nm_id, public_price_rub=0, paid_price_rub=paid_price_rub,
                    spp_percent=0.0, observation_id=None, skipped_reason="wb_fetch_failed",
                )

            if not items:
                return ObserveResult(
                    nm_id=nm_id, public_price_rub=0, paid_price_rub=paid_price_rub,
                    spp_percent=0.0, observation_id=None,
                    skipped_reason="nm_not_found_on_wb",
                )

            public_price = int(items[0].price_rub or 0)
            if public_price <= 0:
                return ObserveResult(
                    nm_id=nm_id, public_price_rub=0, paid_price_rub=paid_price_rub,
                    spp_percent=0.0, observation_id=None,
                    skipped_reason="public_price_zero",
                )

        if paid_price_rub <= 0 or paid_price_rub > public_price:
            return ObserveResult(
                nm_id=nm_id, public_price_rub=public_price,
                paid_price_rub=paid_price_rub, spp_percent=0.0,
                observation_id=None,
                skipped_reason="paid_outside_range",
                wallet_only=wallet_only,
            )

        # Lookup subject from WB card payload — needed for category-AVG SPP
        subject_id, subject_name = await self._fetch_subject_for_nm(nm_id)

        try:
            obs_id = await self._repo.record_spp_observation(
                nm_id=nm_id,
                subject_id=subject_id,
                subject_name=subject_name,
                public_price_rub=public_price,
                my_buyer_price_rub=paid_price_rub,
                source=source,
                confidence="high",
                sample_count=1,
                note=note,
                wallet_only=wallet_only,
            )
        except Exception:
            logger.exception("AutoObserver: failed to record observation for nm=%s", nm_id)
            return ObserveResult(
                nm_id=nm_id, public_price_rub=public_price,
                paid_price_rub=paid_price_rub, spp_percent=0.0,
                observation_id=None, skipped_reason="db_insert_failed",
                wallet_only=wallet_only,
            )

        spp_pct = (1.0 - paid_price_rub / public_price) * 100.0
        logger.info(
            "AutoObserver: nm=%s subj=%s public=%d paid=%d SPP=%.1f%% wallet_only=%s obs=%d",
            nm_id, subject_id, public_price, paid_price_rub, spp_pct, wallet_only, obs_id,
        )
        return ObserveResult(
            nm_id=nm_id, public_price_rub=public_price,
            paid_price_rub=paid_price_rub, spp_percent=spp_pct,
            observation_id=obs_id,
            wallet_only=wallet_only,
        )

    async def _fetch_subject_for_nm(self, nm_id: int) -> tuple[int | None, str | None]:
        """Resolve subjectId and subjectName from raw WB payload.

        Uses search_for_arbitrage_raw with nm_id as query — WB indexes
        products by nm in search results.
        """
        try:
            raw = await self._wb.search_for_arbitrage_raw(str(nm_id), max_pages=1)
        except Exception:
            return None, None
        target = next((p for p in raw if p.get("id") == nm_id), None)
        if not target:
            return None, None
        sid = target.get("subjectId")
        if not isinstance(sid, int):
            return None, None
        name = (target.get("subjectName") or "").strip() or None
        return sid, name
