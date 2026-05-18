"""Auto-fetch buyer-side personal СПП observations.

Three ways to generate ``arb_buyer_spp_observations`` automatically:

1. From a /buy or /addpurchase event — when owner records a real purchase
   we fetch public WB price for that nm_id and store residual СПП.
2. /arb_quickadd <nm> <my_price> — fetch public + compute + save in one shot.
3. /arb_bulk — multiline paste of "nm price" pairs.

All three avoid the buyer-cookie-with-PoW path (deferred). They rely on
real owner-supplied prices, so the observation is ground truth — there
is no estimation, no proxy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.arbitrage.repository import ArbitrageRepository
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
    ) -> None:
        self._wb = wb_client
        self._repo = arb_repo

    async def observe(
        self,
        *,
        nm_id: int,
        paid_price_rub: int,
        source: str = "purchase",
        note: str | None = None,
    ) -> ObserveResult:
        """Fetch public price for nm_id, compute СПП, record observation.

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

        item = items[0]
        public_price = int(item.price_rub or 0)
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
            )
        except Exception:
            logger.exception("AutoObserver: failed to record observation for nm=%s", nm_id)
            return ObserveResult(
                nm_id=nm_id, public_price_rub=public_price,
                paid_price_rub=paid_price_rub, spp_percent=0.0,
                observation_id=None, skipped_reason="db_insert_failed",
            )

        spp_pct = (1.0 - paid_price_rub / public_price) * 100.0
        logger.info(
            "AutoObserver: nm=%s subj=%s public=%d paid=%d SPP=%.1f%% obs=%d",
            nm_id, subject_id, public_price, paid_price_rub, spp_pct, obs_id,
        )
        return ObserveResult(
            nm_id=nm_id, public_price_rub=public_price,
            paid_price_rub=paid_price_rub, spp_percent=spp_pct,
            observation_id=obs_id,
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
