"""Buyer-side СПП resolver (2026-05-18 model).

WB-Скидка (бывш. СПП) is CATEGORY-WIDE, не per-buyer. WB сам финансирует её
(оферта п. 5.4). Это значит: 1 observation per category достаточно чтобы
знать ставку для ВСЕЙ категории.

The observed `spp_percent` is COMPOSITE: includes both WB-Скидка категории
AND owner's WB-Кошелёк bonus (~6%). margin.decompose_composite_spp() splits
them for accurate buy_price math.

Resolution order:
  1. Per-nm observation (last 24h, any confidence) — exact composite for THIS sku.
  2. Per-category AVG (last 30d, ≥1 sample, exclude 'low') — composite for the subject.
  3. Skip (return None) if no data — never alert with assumed default.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.arbitrage.repository import ArbitrageRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SppResolution:
    """Composite buyer discount (category_СПП + wallet bonus combined).

    To decompose into category-only, use margin.decompose_composite_spp().
    """
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
        """Returns composite buyer discount, or None to signal 'skip — no data'."""
        # 1. Exact per-nm observation (within 24h)
        nm_recent = await self._repo.get_nm_recent_spp(nm_id, hours=24)
        if nm_recent and nm_recent["confidence"] != "low":
            return SppResolution(
                spp_percent=nm_recent["spp_percent"],
                source="observation",
                confidence=nm_recent["confidence"],
                note=f"Sampled {nm_recent['observed_at'][:16]}",
            )

        # 2. Category AVG — valid because WB-СПП is category-wide.
        # Single-sample is acceptable confidence=medium (СПП varies 21-25%/month
        # which is well within owner's risk tolerance per his arbitrage model).
        if subject_id is not None:
            cat = await self._repo.get_category_avg_spp(
                subject_id, days=30, min_samples=1,
            )
            if cat:
                samples = cat["samples"]
                # Confidence ladder: 5+ samples = high (variance smoothed),
                # 2-4 = medium, 1 = medium-but-tagged (single observation
                # still trustworthy because the value is category-wide).
                if samples >= 5:
                    confidence = "high"
                elif samples >= 2:
                    confidence = "medium"
                else:
                    confidence = "medium"  # 1-sample still acceptable
                return SppResolution(
                    spp_percent=cat["avg_spp"],
                    source="category_avg",
                    confidence=confidence,
                    note=f"AVG {cat['avg_spp']:.1f}% over {samples} sample(s)",
                )

        # 3. No data at all — skip
        return None
