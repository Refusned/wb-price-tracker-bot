"""
WB Seller API client for business data (sales, orders, stocks, finances).
Designed for FBS-via-WB-warehouses mode (FBS where seller ships to WB warehouse).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp


_STATISTICS_BASE = "https://statistics-api.wildberries.ru"
_MARKETPLACE_BASE = "https://marketplace-api.wildberries.ru"
_ANALYTICS_BASE = "https://seller-analytics-api.wildberries.ru"
_CONTENT_BASE = "https://content-api.wildberries.ru"

# Rate limits per category (conservative)
_STATS_MIN_INTERVAL = 6.0   # 10 req / 60 sec for statistics-api
_MARKETPLACE_MIN_INTERVAL = 0.5
_ANALYTICS_MIN_INTERVAL = 6.0

# Остатки FBO: эндпоинт /api/v1/supplier/stocks отдаёт real-time снимок без
# истории и фильтрует строки по lastChangeDate >= dateFrom. Чтобы получить ВСЕ
# остатки (в т.ч. товары без недавних движений), стартуем с даты раньше запуска
# статистики WB. Лимит ответа — 60000 строк (дальше нужна пагинация).
_STOCKS_EPOCH = datetime(2019, 6, 20)
_STOCKS_ROW_LIMIT = 60000


class SellerApiError(Exception):
    """WB Seller API недоступен после ретраев.

    Бросается вместо тихого возврата []: пустой список неотличим от
    "событий нет", из-за чего планировщик помечал цикл успешным и терял
    данные. С исключением цикл корректно падает в False, а
    last_seller_update_at не обновляется (видно staleness в /status).
    """


@dataclass(slots=True)
class SaleEvent:
    """A sale OR return event (is_return=True if return)."""
    g_number: str
    date: str
    last_change_date: str
    nm_id: int
    supplier_article: str
    subject: str
    brand: str
    category: str
    warehouse_name: str
    barcode: str
    total_price: float
    for_pay: float
    price_with_disc: float
    spp_percent: float
    commission_percent: float
    discount_percent: float
    is_return: bool
    srid: str
    order_type: str | None = None


@dataclass(slots=True)
class OrderEvent:
    g_number: str
    date: str
    last_change_date: str
    nm_id: int
    supplier_article: str
    subject: str
    warehouse_name: str
    barcode: str
    total_price: float
    price_with_disc: float
    spp_percent: float
    discount_percent: float
    is_cancel: bool
    cancel_date: str | None
    srid: str


@dataclass(slots=True)
class StockEntry:
    nm_id: int
    supplier_article: str
    warehouse_name: str
    quantity: int
    in_way_to_client: int
    in_way_from_client: int
    quantity_full: int
    subject: str
    last_change_date: str


class SellerClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._logger = logging.getLogger(self.__class__.__name__)

        self._stats_lock = asyncio.Lock()
        self._stats_last_ts = 0.0
        self._market_lock = asyncio.Lock()
        self._market_last_ts = 0.0
        self._analytics_lock = asyncio.Lock()
        self._analytics_last_ts = 0.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _throttle(self, kind: str) -> None:
        if kind == "stats":
            lock, attr, interval = self._stats_lock, "_stats_last_ts", _STATS_MIN_INTERVAL
        elif kind == "market":
            lock, attr, interval = self._market_lock, "_market_last_ts", _MARKETPLACE_MIN_INTERVAL
        else:
            lock, attr, interval = self._analytics_lock, "_analytics_last_ts", _ANALYTICS_MIN_INTERVAL
        async with lock:
            import time
            now = time.monotonic()
            last = getattr(self, attr)
            elapsed = now - last
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            setattr(self, attr, time.monotonic())

    # ---------- Statistics API ----------

    async def get_sales(self, date_from: datetime, *, flag: int = 0) -> list[SaleEvent]:
        """Sales and returns. Updated every 30 min. flag=0: changed since; flag=1: all on date."""
        params = {"dateFrom": date_from.strftime("%Y-%m-%dT%H:%M:%S"), "flag": str(flag)}
        url = f"{_STATISTICS_BASE}/api/v1/supplier/sales"
        data: list = []
        got = False
        last_exc: Exception | None = None
        for attempt in range(3):
            await self._throttle("stats")
            try:
                async with self._session.get(url, params=params, headers=self._headers, timeout=self._timeout) as resp:
                    if resp.status == 429:
                        backoff = 10 * (2 ** attempt)  # 10, 20, 40s
                        self._logger.warning("Sales API 429 — retry in %ds (attempt %d)", backoff, attempt + 1)
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    data = await resp.json() or []
                    got = True
                    break
            except aiohttp.ClientError as e:
                last_exc = e
                self._logger.warning("get_sales failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(5)
        if not got:
            # Все попытки исчерпаны. НЕ возвращаем [] — оно неотличимо от
            # "продаж нет"; бросаем, чтобы цикл пометился failed.
            raise SellerApiError("get_sales failed after 3 attempts") from last_exc
        if not isinstance(data, list):
            data = []

        events: list[SaleEvent] = []
        for s in data:
            try:
                events.append(SaleEvent(
                    g_number=str(s.get("gNumber", "")),
                    date=str(s.get("date", "")),
                    last_change_date=str(s.get("lastChangeDate", "")),
                    nm_id=int(s.get("nmId", 0)),
                    supplier_article=str(s.get("supplierArticle", "")),
                    subject=str(s.get("subject", "")),
                    brand=str(s.get("brand", "")),
                    category=str(s.get("category", "")),
                    warehouse_name=str(s.get("warehouseName", "")),
                    barcode=str(s.get("barcode", "")),
                    total_price=float(s.get("totalPrice", 0)),
                    for_pay=float(s.get("forPay", 0)),
                    price_with_disc=float(s.get("priceWithDisc", 0)),
                    spp_percent=float(s.get("spp", 0)),
                    commission_percent=float(s.get("commissionPercent", 0)),
                    discount_percent=float(s.get("discountPercent", 0)),
                    is_return=str(s.get("saleID", "")).startswith("R"),
                    srid=str(s.get("srid", "")),
                    order_type=s.get("orderType"),
                ))
            except (KeyError, ValueError, TypeError) as e:
                self._logger.debug("Skip malformed sale: %s", e)
        return events

    async def get_orders(self, date_from: datetime, *, flag: int = 0) -> list[OrderEvent]:
        """All orders (including canceled). Updated every 30 min."""
        params = {"dateFrom": date_from.strftime("%Y-%m-%dT%H:%M:%S"), "flag": str(flag)}
        url = f"{_STATISTICS_BASE}/api/v1/supplier/orders"
        data: list = []
        got = False
        last_exc: Exception | None = None
        for attempt in range(3):
            await self._throttle("stats")
            try:
                async with self._session.get(url, params=params, headers=self._headers, timeout=self._timeout) as resp:
                    if resp.status == 429:
                        backoff = 10 * (2 ** attempt)
                        self._logger.warning("Orders API 429 — retry in %ds", backoff)
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    data = await resp.json() or []
                    got = True
                    break
            except aiohttp.ClientError as e:
                last_exc = e
                self._logger.warning("get_orders failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(5)
        if not got:
            raise SellerApiError("get_orders failed after 3 attempts") from last_exc
        if not isinstance(data, list):
            data = []

        events: list[OrderEvent] = []
        for o in data:
            try:
                events.append(OrderEvent(
                    g_number=str(o.get("gNumber", "")),
                    date=str(o.get("date", "")),
                    last_change_date=str(o.get("lastChangeDate", "")),
                    nm_id=int(o.get("nmId", 0)),
                    supplier_article=str(o.get("supplierArticle", "")),
                    subject=str(o.get("subject", "")),
                    warehouse_name=str(o.get("warehouseName", "")),
                    barcode=str(o.get("barcode", "")),
                    total_price=float(o.get("totalPrice", 0)),
                    price_with_disc=float(o.get("priceWithDisc", 0)),
                    spp_percent=float(o.get("spp", 0)),
                    discount_percent=float(o.get("discountPercent", 0)),
                    is_cancel=bool(o.get("isCancel", False)),
                    cancel_date=o.get("cancelDate"),
                    srid=str(o.get("srid", "")),
                ))
            except (KeyError, ValueError, TypeError):
                pass
        return events

    async def get_stocks(self, date_from: datetime | None = None) -> list[StockEntry]:
        """ПОЛНЫЙ real-time снимок остатков FBO по складам WB (обновляется каждые 30 мин).

        dateFrom здесь — не окно истории, а нижняя граница по lastChangeDate;
        для полного снимка стартуем с _STOCKS_EPOCH. Контракт как у
        get_sales/get_orders: 3 попытки, при неустранимом сбое — SellerApiError;
        пустой список = остатков реально нет (а не «сбой API»).
        """
        cursor = date_from or _STOCKS_EPOCH
        params = {"dateFrom": cursor.strftime("%Y-%m-%dT%H:%M:%S")}
        url = f"{_STATISTICS_BASE}/api/v1/supplier/stocks"
        data: list = []
        got = False
        last_exc: Exception | None = None
        for attempt in range(3):
            await self._throttle("stats")
            try:
                async with self._session.get(url, params=params, headers=self._headers, timeout=self._timeout) as resp:
                    if resp.status == 429:
                        backoff = 10 * (2 ** attempt)  # 10, 20, 40s
                        self._logger.warning("Stocks API 429 — retry in %ds (attempt %d)", backoff, attempt + 1)
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    data = await resp.json() or []
                    got = True
                    break
            except aiohttp.ClientError as e:
                last_exc = e
                self._logger.warning("get_stocks failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(5)
        if not got:
            # Все попытки исчерпаны. НЕ возвращаем [] — оно неотличимо от
            # "остатков нет"; бросаем, чтобы планировщик пропустил purge.
            raise SellerApiError("get_stocks failed after 3 attempts") from last_exc
        if not isinstance(data, list):
            data = []
        if len(data) >= _STOCKS_ROW_LIMIT:
            # Упёрлись в лимит строк WB → снимок усечён и НЕПОЛНЫЙ. Бросаем, а не
            # возвращаем усечённое: иначе планировщик счёл бы FBO успешным
            # (fbo_ok=True) и purge снёс бы валидные остатки за пределами лимита.
            # Полная поддержка >60k требует пагинации по lastChangeDate.
            raise SellerApiError(
                f"get_stocks: ответ упёрся в лимит {_STOCKS_ROW_LIMIT} строк — нужна пагинация"
            )

        entries: list[StockEntry] = []
        for s in data:
            try:
                entries.append(StockEntry(
                    nm_id=int(s.get("nmId", 0)),
                    supplier_article=str(s.get("supplierArticle", "")),
                    warehouse_name=str(s.get("warehouseName", "")),
                    quantity=int(s.get("quantity", 0)),
                    in_way_to_client=int(s.get("inWayToClient", 0)),
                    in_way_from_client=int(s.get("inWayFromClient", 0)),
                    quantity_full=int(s.get("quantityFull", 0)),
                    subject=str(s.get("subject", "")),
                    last_change_date=str(s.get("lastChangeDate", "")),
                ))
            except (KeyError, ValueError, TypeError):
                pass
        return entries

    async def get_financial_report(self, date_from: datetime, date_to: datetime) -> list[dict[str, Any]]:
        """Financial report: commission, logistics, storage, fines, returns etc.
        Endpoint /api/v5/supplier/reportDetailByPeriod — полный journal всех операций.
        """
        params = {
            "dateFrom": date_from.strftime("%Y-%m-%d"),
            "dateTo": date_to.strftime("%Y-%m-%d"),
            "limit": "100000",
        }
        url = f"{_STATISTICS_BASE}/api/v5/supplier/reportDetailByPeriod"
        data: list = []
        for attempt in range(3):
            await self._throttle("stats")
            try:
                async with self._session.get(url, params=params, headers=self._headers, timeout=self._timeout) as resp:
                    if resp.status == 429:
                        backoff = 10 * (2 ** attempt)
                        self._logger.warning("FinanceReport 429 — retry %ds", backoff)
                        await asyncio.sleep(backoff)
                        continue
                    resp.raise_for_status()
                    data = await resp.json() or []
                    break
            except aiohttp.ClientError as e:
                self._logger.warning("get_financial_report failed (attempt %d): %s", attempt + 1, e)
                await asyncio.sleep(5)
        return data if isinstance(data, list) else []

    # ---------- Marketplace API (FBS) ----------

    async def get_warehouses(self) -> list[dict[str, Any]]:
        """Seller's FBS warehouses."""
        await self._throttle("market")
        url = f"{_MARKETPLACE_BASE}/api/v3/warehouses"
        try:
            async with self._session.get(url, headers=self._headers, timeout=self._timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
                # 200 с телом не-list (деградация WB) не должна ронять FBS-цикл
                # AttributeError'ом в итерации по складам — отдаём пустой список.
                return data if isinstance(data, list) else []
        except aiohttp.ClientError as e:
            # Сетевой сбой не глотаем: иначе FBS-снимок выглядел бы пустым и
            # спровоцировал бы purge валидных остатков (см. get_all_fbs_stocks).
            raise SellerApiError("get_warehouses failed") from e

    async def get_fbs_stocks(self, warehouse_id: int, skus: list[str]) -> list[dict[str, Any]]:
        """FBS stocks for specific warehouse. POST body = list of SKUs (barcodes)."""
        if not skus:
            return []
        await self._throttle("market")
        url = f"{_MARKETPLACE_BASE}/api/v3/stocks/{warehouse_id}"
        try:
            async with self._session.post(
                url,
                json={"skus": skus},
                headers=self._headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    self._logger.warning("get_fbs_stocks wh=%s status=%s body=%s", warehouse_id, resp.status, txt[:200])
                    return []
                data = await resp.json()
                return data.get("stocks", []) if isinstance(data, dict) else []
        except aiohttp.ClientError as e:
            # Сетевой сбой склада пробрасываем, чтобы get_all_fbs_stocks вернул
            # ok=False и planner не пуржил частичный снимок.
            raise SellerApiError(f"get_fbs_stocks wh={warehouse_id} failed") from e

    async def get_content_cards(self) -> list[dict[str, Any]]:
        """Все карточки продавца (content-api). Возвращает cards с sizes/skus/supplier_article/nm_id."""
        url = f"{_CONTENT_BASE}/content/v2/get/cards/list"
        all_cards: list[dict[str, Any]] = []
        cursor: dict[str, Any] = {"limit": 100}
        for _ in range(100):  # safety cap
            body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
            await self._throttle("market")
            try:
                async with self._session.post(url, json=body, headers=self._headers, timeout=self._timeout) as resp:
                    if resp.status != 200:
                        body_preview = (await resp.text())[:200]
                        # Карточки критичны для маппинга sku→nm_id: неполный
                        # маппинг = потерянные FBS-остатки. Сбой → SellerApiError.
                        raise SellerApiError(f"get_content_cards status={resp.status} body={body_preview}")
                    data = await resp.json()
            except aiohttp.ClientError as e:
                raise SellerApiError("get_content_cards failed") from e
            cards = data.get("cards", []) if isinstance(data, dict) else []
            all_cards.extend(cards)
            next_cursor = data.get("cursor", {}) if isinstance(data, dict) else {}
            if len(cards) < 100 or not next_cursor.get("updatedAt"):
                break
            cursor = {
                "limit": 100,
                "updatedAt": next_cursor["updatedAt"],
                "nmID": next_cursor.get("nmID"),
            }
        return all_cards

    async def get_all_fbs_stocks(self) -> tuple[list[StockEntry], bool]:
        """Агрегированные FBS-остатки: карточки → barcodes → остатки по складам.

        Возвращает (остатки, ok). ok=False — был сетевой сбой источника, тогда
        planner НЕ должен пуржить (иначе снесёт валидные строки). ok=True с
        пустым списком = у продавца реально нет FBS-склада/остатков.
        """
        try:
            warehouses = await self.get_warehouses()
            cards = await self.get_content_cards()
        except SellerApiError as e:
            self._logger.warning("FBS meta fetch failed (skip purge): %s", e)
            return [], False
        if not warehouses or not cards:
            self._logger.info("FBS stocks: warehouses=%d cards=%d", len(warehouses), len(cards))
            return [], True

        # Map barcode → (nm_id, supplier_article, subject)
        sku_meta: dict[str, dict[str, Any]] = {}
        for card in cards:
            nm_id = card.get("nmID") or 0
            art = card.get("vendorCode") or ""
            subject = card.get("subjectName") or ""
            for size in (card.get("sizes") or []):
                for sku in (size.get("skus") or []):
                    sku_meta[str(sku)] = {"nm_id": int(nm_id), "art": str(art), "subject": str(subject)}
        skus = list(sku_meta.keys())
        if not skus:
            return [], True

        entries: list[StockEntry] = []
        try:
            for wh in warehouses:
                wh_id = wh.get("id")
                wh_name = wh.get("name", f"FBS-{wh_id}")
                if not wh_id:
                    continue
                # WB limit: 1000 SKU per POST
                for i in range(0, len(skus), 1000):
                    chunk = skus[i:i + 1000]
                    stocks = await self.get_fbs_stocks(wh_id, chunk)
                    for s in stocks:
                        try:
                            sku = str(s.get("sku", ""))
                            meta = sku_meta.get(sku, {})
                            qty = int(s.get("amount", 0))
                            if qty == 0:
                                continue  # skip zero-stock rows
                            entries.append(StockEntry(
                                nm_id=int(meta.get("nm_id", 0) or 0),
                                supplier_article=str(meta.get("art") or sku),
                                warehouse_name=wh_name,
                                quantity=qty,
                                in_way_to_client=0,
                                in_way_from_client=0,
                                quantity_full=qty,
                                subject=str(meta.get("subject", "")),
                                last_change_date="",
                            ))
                        except (KeyError, ValueError, TypeError):
                            pass
        except SellerApiError as e:
            self._logger.warning("FBS stock fetch failed (skip purge): %s", e)
            return [], False
        return entries, True

    async def get_new_fbs_orders(self) -> list[dict[str, Any]]:
        """New FBS orders that need to be assembled."""
        await self._throttle("market")
        url = f"{_MARKETPLACE_BASE}/api/v3/orders/new"
        try:
            async with self._session.get(url, headers=self._headers, timeout=self._timeout) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return data.get("orders", []) if isinstance(data, dict) else []
        except aiohttp.ClientError as e:
            self._logger.warning("get_new_fbs_orders failed: %s", e)
            return []

    # ---------- Analytics API ----------

    async def get_nm_report_detail(
        self,
        nm_ids: list[int],
        date_from: datetime,
        date_to: datetime,
    ) -> list[dict[str, Any]]:
        """Detailed metrics per article: views, cart adds, orders, buyouts, etc."""
        await self._throttle("analytics")
        url = f"{_ANALYTICS_BASE}/api/v2/nm-report/detail"
        payload = {
            "period": {
                "begin": date_from.strftime("%Y-%m-%d %H:%M:%S"),
                "end": date_to.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "nmIDs": nm_ids,
            "page": 1,
        }
        try:
            async with self._session.post(url, json=payload, headers=self._headers, timeout=self._timeout) as resp:
                if resp.status == 429:
                    return []
                resp.raise_for_status()
                data = await resp.json()
                return data.get("data", {}).get("cards", []) if isinstance(data, dict) else []
        except aiohttp.ClientError as e:
            self._logger.warning("get_nm_report_detail failed: %s", e)
            return []
