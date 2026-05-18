"""Daily refresh of WB FBS commission + box tariffs.

Endpoints (verified by PoC-gate #2 on 2026-05-18):
    GET https://common-api.wildberries.ru/api/v1/tariffs/commission?locale=ru
    GET https://common-api.wildberries.ru/api/v1/tariffs/box?date=YYYY-MM-DD

Both require Authorization header with WB_SELLER_API_KEY (JWT).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from app.arbitrage.tariffs_repository import TariffsRepository

logger = logging.getLogger(__name__)

COMMISSION_URL = "https://common-api.wildberries.ru/api/v1/tariffs/commission"
BOX_URL = "https://common-api.wildberries.ru/api/v1/tariffs/box"


class TariffsCache:
    """Refreshes WB FBS tariffs from common-api once per day, stores in DB."""

    def __init__(
        self,
        *,
        session: aiohttp.ClientSession,
        seller_api_key: str,
        tariffs_repo: TariffsRepository,
        refresh_interval_seconds: int = 24 * 3600,
        request_timeout_seconds: float = 30.0,
    ) -> None:
        self._session = session
        self._api_key = seller_api_key
        self._repo = tariffs_repo
        self._refresh_interval = refresh_interval_seconds
        self._timeout = request_timeout_seconds
        self._last_refresh_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def refresh_if_stale(self) -> bool:
        """Trigger refresh if last_refresh was more than refresh_interval ago.

        Returns True if a refresh ran, False if cache was still fresh.
        """
        if not self.is_configured:
            return False
        now = datetime.now(timezone.utc)
        if self._last_refresh_at is not None:
            age = (now - self._last_refresh_at).total_seconds()
            if age < self._refresh_interval:
                return False

        async with self._lock:
            # Re-check inside lock to avoid duplicate refresh
            if self._last_refresh_at is not None:
                age = (now - self._last_refresh_at).total_seconds()
                if age < self._refresh_interval:
                    return False
            await self._refresh_commission()
            await self._refresh_box()
            self._last_refresh_at = now
        return True

    async def force_refresh(self) -> dict[str, int]:
        """Forced refresh (for manual /arb_refresh_tariffs). Returns counts."""
        if not self.is_configured:
            return {"commission": 0, "box": 0}
        async with self._lock:
            c = await self._refresh_commission()
            b = await self._refresh_box()
            self._last_refresh_at = datetime.now(timezone.utc)
        return {"commission": c, "box": b}

    async def _refresh_commission(self) -> int:
        headers = {"Authorization": self._api_key}
        try:
            async with self._session.get(
                COMMISSION_URL,
                params={"locale": "ru"},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("tariffs/commission HTTP %d: %s", resp.status, body[:200])
                    return 0
                payload = await resp.json()
        except Exception:
            logger.exception("tariffs/commission fetch failed")
            return 0

        rows = _extract_commission_rows(payload)
        if not rows:
            logger.warning("tariffs/commission returned 0 rows")
            return 0
        await self._repo.upsert_commission(rows)
        logger.info("tariffs/commission refreshed: %d subjects", len(rows))
        return len(rows)

    async def _refresh_box(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        headers = {"Authorization": self._api_key}
        try:
            async with self._session.get(
                BOX_URL,
                params={"date": today},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("tariffs/box HTTP %d: %s", resp.status, body[:200])
                    return 0
                payload = await resp.json()
        except Exception:
            logger.exception("tariffs/box fetch failed")
            return 0

        warehouses = _extract_box_warehouses(payload)
        if not warehouses:
            logger.warning("tariffs/box returned 0 warehouses")
            return 0
        await self._repo.upsert_box(warehouses)
        logger.info("tariffs/box refreshed: %d warehouses", len(warehouses))
        return len(warehouses)


def _extract_commission_rows(payload: Any) -> list[dict[str, Any]]:
    """Parse `/tariffs/commission` response.

    The WB endpoint can return either ``{"report": [...]}`` (older) or just a
    list at the root (newer). We handle both defensively.
    """
    if isinstance(payload, dict):
        report = payload.get("report") or payload.get("data") or payload.get("response")
        if isinstance(report, list):
            return [r for r in report if isinstance(r, dict)]
        if isinstance(report, dict):
            inner = report.get("data") or report.get("report")
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    return []


def _extract_box_warehouses(payload: Any) -> list[dict[str, Any]]:
    """Parse `/tariffs/box` response: ``{"response": {"data": {"warehouseList": [...]}}}``."""
    if not isinstance(payload, dict):
        return []
    resp = payload.get("response", payload)
    if not isinstance(resp, dict):
        return []
    data = resp.get("data", resp)
    if not isinstance(data, dict):
        return []
    wl = data.get("warehouseList")
    if isinstance(wl, list):
        return [w for w in wl if isinstance(w, dict)]
    return []
