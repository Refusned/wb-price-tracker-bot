"""Buyer-side personal СПП resolver.

Round 4 Plan B: cookie path deferred (card.wb.ru requires PoW). Canonical
sources for buyer-side СПП:
    1. PER-NM_ID observation (manual /arb_observe or auto on /buy): exact
       value if observed within last 24h.
    2. PER-CATEGORY average across all observations in subject (≥3 samples,
       within 30 days). Confidence 'medium'.
    3. Manual category-override via /arb_set_spp <subject> <pct>. NOT
       implemented in MVP — falls through to skip.

Skip (do NOT alert) if no observation can be resolved with confidence.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.arbitrage.repository import ArbitrageRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SppResolution:
    spp_percent: float
    source: str        # 'observation' | 'category_avg' | 'manual'
    confidence: str    # 'high' | 'medium' | 'low'
    note: str = ""


class PersonalSppResolver:
    def __init__(self, repo: ArbitrageRepository) -> None:
        self._repo = repo

    async def resolve(
        self, *, nm_id: int, subject_id: int | None,
    ) -> SppResolution | None:
        """Returns SppResolution or None to signal 'skip — no reliable СПП'.

        Round 4: NO 'default 20%' fallback that alerts (Codex D13). If we
        can't measure СПП for this category, we don't pretend.
        """
        # 1. Exact nm_id observation (last 24h)
        nm_recent = await self._repo.get_nm_recent_spp(nm_id, hours=24)
        if nm_recent and nm_recent["confidence"] != "low":
            return SppResolution(
                spp_percent=nm_recent["spp_percent"],
                source="observation",
                confidence=nm_recent["confidence"],
                note=f"Sampled {nm_recent['observed_at'][:16]}",
            )

        # 2. Category average (need subject_id and ≥3 samples)
        if subject_id is not None:
            cat = await self._repo.get_category_avg_spp(
                subject_id, days=30, min_samples=3,
            )
            if cat:
                # Codex D16 safety: при category fallback вычитаем 3 п.п.
                # bias safety margin (consrervative — занижаем СПП → margin меньше)
                conservative_spp = max(0.0, cat["avg_spp"] - 3.0)
                confidence = "medium" if cat["samples"] >= 5 else "low"
                # Round 4: skip low confidence
                if confidence == "low":
                    return None
                return SppResolution(
                    spp_percent=conservative_spp,
                    source="category_avg",
                    confidence=confidence,
                    note=f"AVG {cat['avg_spp']:.1f}% over {cat['samples']} samples (-3pp safety)",
                )

        # 3. Skip — no reliable source
        return None
