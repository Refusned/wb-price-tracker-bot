from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import aiohttp

from app.storage.models import Item
from app.utils.retry import retry_async
from app.wb.endpoints import SEARCH_ENDPOINTS, SearchEndpoint
from app.wb.parser import normalize_price, parse_products


class WildberriesClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout_seconds: float = 12.0,
        retries: int = 3,
        backoff_seconds: float = 0.5,
        rate_limit_rps: float = 1.5,
        max_pages: int = 3,
        exclude_keywords: list[str] | None = None,
    ) -> None:
        self._session = session
        self._timeout_seconds = timeout_seconds
        self._retries = retries
        self._backoff_seconds = backoff_seconds
        self._max_pages = max(1, max_pages)
        self._min_interval = 1.0 / max(rate_limit_rps, 0.1)
        self._exclude_keywords = [k.lower() for k in (exclude_keywords or []) if k.strip()]

        self._logger = logging.getLogger(self.__class__.__name__)
        self._rate_lock = asyncio.Lock()
        self._last_request_ts = 0.0
        self._card_rate_lock = asyncio.Lock()
        self._card_last_request_ts = 0.0
        self._card_min_interval = 1.0

        self._headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
            "Connection": "keep-alive",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
        }

    async def search_across_queries(
        self,
        *,
        queries: list[str],
        max_pages: int | None = None,
    ) -> list[Item]:
        normalized_queries = self._normalize_queries(queries)
        if not normalized_queries:
            raise ValueError("No WB queries provided")

        merged_items: dict[str, Item] = {}
        successful_queries = 0

        for index, query in enumerate(normalized_queries):
            if index > 0:
                await asyncio.sleep(0.3)

            try:
                query_items = await self.search(query=query, max_pages=max_pages)
            except Exception as exc:
                self._logger.warning("WB query failed: '%s' (%s)", query, exc)
                continue

            successful_queries += 1
            for item in query_items:
                existing = merged_items.get(item.nm_id)
                if existing is None:
                    merged_items[item.nm_id] = item
                else:
                    self._merge_items(existing, item)

        if successful_queries == 0:
            raise RuntimeError(f"WB search failed for all queries: {normalized_queries}")

        return list(merged_items.values())

    async def fetch_cards_batch(
        self,
        nm_ids: list[str],
        *,
        batch_size: int = 25,
    ) -> list[Item]:
        if not nm_ids:
            return []

        all_items: list[Item] = []

        for start in range(0, len(nm_ids), batch_size):
            batch = nm_ids[start : start + batch_size]
            nm_param = ";".join(batch)

            try:
                await self._respect_card_rate_limit()
                params = {
                    "appType": "1",
                    "curr": "rub",
                    "dest": "-1257786",
                    "spp": "30",
                    "nm": nm_param,
                }
                timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
                async with self._session.get(
                    "https://card.wb.ru/cards/v4/detail",
                    params=params,
                    headers=self._headers,
                    timeout=timeout,
                ) as response:
                    if response.status >= 400:
                        self._logger.warning(
                            "Card batch failed: HTTP %s for %d items",
                            response.status,
                            len(batch),
                        )
                        continue
                    payload = await response.json(content_type=None)

                products = payload.get("products", []) if isinstance(payload, dict) else []
                for product in products:
                    if not isinstance(product, dict):
                        continue

                    nm_id = str(product.get("id", ""))
                    if not nm_id:
                        continue

                    current_price = self._extract_card_current_price(product)
                    if current_price is None:
                        continue

                    old_price = self._extract_card_old_price(product, current_price)
                    stock_qty = self._extract_card_stock_qty(product)
                    in_stock = stock_qty > 0 if stock_qty is not None else True
                    name = product.get("name", f"WB {nm_id}")
                    if isinstance(name, str):
                        name = name.strip()

                    item = Item(
                        nm_id=nm_id,
                        name=name or f"WB {nm_id}",
                        price_rub=current_price,
                        old_price_rub=old_price,
                        in_stock=in_stock,
                        stock_qty=stock_qty,
                        url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
                    )

                    all_items.append(item)

            except Exception as exc:
                self._logger.warning("Card batch error for %d items: %s", len(batch), exc)

            if start + batch_size < len(nm_ids):
                await asyncio.sleep(1.0)

        self._logger.info("Card batch: fetched %d items from %d nm_ids", len(all_items), len(nm_ids))
        return all_items

    async def search(self, query: str, max_pages: int | None = None) -> list[Item]:
        pages = max_pages if max_pages is not None else self._max_pages
        pages = max(1, pages)

        unique_items: dict[str, Item] = {}

        for page in range(1, pages + 1):
            endpoint_responded = False
            page_items: list[Item] = []

            for endpoint in SEARCH_ENDPOINTS:
                try:
                    endpoint_items = await self._fetch_page_items(endpoint, query=query, page=page)
                    endpoint_responded = True
                    if endpoint_items:
                        page_items = endpoint_items
                        self._logger.debug(
                            "WB endpoint=%s page=%s parsed_items=%s",
                            endpoint.name,
                            page,
                            len(page_items),
                        )
                        break
                except Exception as exc:
                    self._logger.debug(
                        "WB endpoint=%s page=%s failed: %s",
                        endpoint.name,
                        page,
                        exc,
                    )

            if not endpoint_responded:
                if page == 1:
                    raise RuntimeError(f"WB search failed for query '{query}'")
                break

            if not page_items:
                break

            for item in page_items:
                unique_items[item.nm_id] = item

        return list(unique_items.values())

    async def _fetch_page_items(self, endpoint: SearchEndpoint, *, query: str, page: int) -> list[Item]:
        payload = await self._request_endpoint_page(endpoint, query=query, page=page)
        items = self._filter_relevant_items(
                parse_products(payload), query=query, exclude_keywords=self._exclude_keywords,
            )
        if items:
            return items

        shard_key, routed_query = self._extract_routing(payload, fallback_query=query)
        if self._should_try_shard_route(shard_key=shard_key, routed_query=routed_query):
            routed_payload = await self._request_shard_page(
                endpoint,
                shard_key=shard_key or "",
                query=routed_query or "",
                page=page,
            )
            routed_items = self._filter_relevant_items(
                parse_products(routed_payload), query=query, exclude_keywords=self._exclude_keywords,
            )
            if routed_items:
                self._logger.debug(
                    "WB shard route resolved endpoint=%s page=%s shard=%s items=%s",
                    endpoint.name,
                    page,
                    shard_key,
                    len(routed_items),
                )
                return routed_items

        if self._is_routing_payload(payload):
            raise RuntimeError("WB returned only routing metadata without products")

        return []

    async def _request_endpoint_page(self, endpoint: SearchEndpoint, *, query: str, page: int) -> dict:
        params = endpoint.build_params(query=query, page=page)
        return await self._request_json(
            url=endpoint.url,
            params=params,
            operation_name=f"WB request {endpoint.name} page={page}",
        )

    async def _request_shard_page(
        self,
        endpoint: SearchEndpoint,
        *,
        shard_key: str,
        query: str,
        page: int,
    ) -> dict:
        shard = shard_key.strip().strip("/")
        url = f"https://search.wb.ru/{shard}/v{endpoint.version}/search"
        params = endpoint.build_params(query=query, page=page)
        return await self._request_json(
            url=url,
            params=params,
            operation_name=f"WB shard request {endpoint.name} page={page}",
        )

    async def _request_json(self, *, url: str, params: dict[str, str], operation_name: str) -> dict:
        async def _attempt() -> dict:
            await self._respect_rate_limit()
            timeout = aiohttp.ClientTimeout(total=self._timeout_seconds)
            async with self._session.get(
                url,
                params=params,
                headers=self._headers,
                timeout=timeout,
            ) as response:
                if response.status >= 400:
                    body_preview = (await response.text())[:300]
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"HTTP {response.status}: {body_preview}",
                        headers=response.headers,
                    )
                return await response.json(content_type=None)

        return await retry_async(
            _attempt,
            retries=self._retries,
            base_delay=self._backoff_seconds,
            exceptions=(aiohttp.ClientError, asyncio.TimeoutError),
            logger=self._logger,
            operation_name=operation_name,
        )

    @staticmethod
    def _extract_routing(payload: Any, *, fallback_query: str) -> tuple[str | None, str | None]:
        if not isinstance(payload, dict):
            return None, None

        metadata = payload.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}

        shard_key = payload.get("shardKey") or meta.get("shardKey") or meta.get("shardkey")
        query = payload.get("query") or meta.get("catalog_value") or fallback_query

        shard_text = str(shard_key).strip() if shard_key else None
        query_text = str(query).strip() if query else None
        return shard_text, query_text

    @staticmethod
    def _should_try_shard_route(*, shard_key: str | None, routed_query: str | None) -> bool:
        if not shard_key or not routed_query:
            return False

        query_lower = routed_query.casefold()
        # WB often returns technical preset query values that do not resolve via shard URL.
        if query_lower.startswith("preset="):
            return False
        if "&" in routed_query or "%26" in query_lower:
            return False
        return True

    @staticmethod
    def _is_routing_payload(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False

        metadata = payload.get("metadata")
        meta = metadata if isinstance(metadata, dict) else {}
        shard_key = payload.get("shardKey") or meta.get("shardKey") or meta.get("shardkey")
        query = payload.get("query") or meta.get("catalog_value")

        if shard_key:
            return True
        if isinstance(query, str) and (
            query.startswith("preset=") or query.startswith("subject=") or query.startswith("brand=")
        ):
            return True
        return False

    @staticmethod
    def _normalize_queries(queries: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for query in queries:
            cleaned = query.strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(cleaned)
        return normalized

    @staticmethod
    def _merge_items(existing: Item, incoming: Item) -> None:
        if incoming.price_rub < existing.price_rub:
            existing.price_rub = incoming.price_rub
            existing.url = incoming.url or existing.url

        if (
            incoming.old_price_rub is not None
            and (existing.old_price_rub is None or incoming.old_price_rub > existing.old_price_rub)
        ):
            existing.old_price_rub = incoming.old_price_rub

        if len(incoming.name) > len(existing.name):
            existing.name = incoming.name

        existing.in_stock = existing.in_stock or incoming.in_stock
        if incoming.stock_qty is not None:
            if existing.stock_qty is None or incoming.stock_qty > existing.stock_qty:
                existing.stock_qty = incoming.stock_qty

    @staticmethod
    def _filter_relevant_items(
        items: list[Item], *, query: str, exclude_keywords: list[str] | None = None,
    ) -> list[Item]:
        # First pass: exclude items whose name contains any blocked keyword
        # (e.g., color filters configured via TOP10_EXCLUDE_KEYWORDS).
        if exclude_keywords:
            items = [
                item for item in items
                if not any(kw in item.name.lower() for kw in exclude_keywords)
            ]

        tokens = [token for token in re.findall(r"[\w\d]+", query.lower()) if len(token) >= 3]
        if not tokens:
            return items

        strict = [item for item in items if all(token in item.name.lower() for token in tokens)]
        if strict:
            return strict

        return [item for item in items if any(token in item.name.lower() for token in tokens)]

    async def _fetch_card_product(self, nm_id: str) -> dict[str, Any] | None:
        if not nm_id.isdigit():
            return None

        params = {
            "appType": "1",
            "curr": "rub",
            "dest": "-1257786",
            "spp": "30",
            "nm": nm_id,
        }
        url = "https://card.wb.ru/cards/v4/detail"

        async def _attempt() -> dict[str, Any]:
            await self._respect_card_rate_limit()
            timeout = aiohttp.ClientTimeout(total=min(self._timeout_seconds, 8.0))
            async with self._session.get(
                url,
                params=params,
                headers=self._headers,
                timeout=timeout,
            ) as response:
                if response.status == 404:
                    return {}
                if response.status >= 400:
                    body_preview = (await response.text())[:300]
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"HTTP {response.status}: {body_preview}",
                        headers=response.headers,
                    )
                payload = await response.json(content_type=None)
                return payload if isinstance(payload, dict) else {}

        try:
            payload = await retry_async(
                _attempt,
                retries=min(self._retries, 3),
                base_delay=0.5,
                exceptions=(aiohttp.ClientError, asyncio.TimeoutError),
                logger=self._logger,
                operation_name=f"WB card detail nm={nm_id}",
            )
        except Exception:
            return None

        products = payload.get("products")
        if not isinstance(products, list):
            return None
        for product in products:
            if isinstance(product, dict):
                return product
        return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
                return int(text)
        return None

    @staticmethod
    def _extract_card_current_price(product: dict[str, Any]) -> float | None:
        candidates: list[float] = []

        sizes = product.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                price_block = size.get("price")
                if isinstance(price_block, dict):
                    for key in ("product", "sale", "total"):
                        normalized = normalize_price(price_block.get(key))
                        if normalized is not None:
                            candidates.append(normalized)
                            break

        for key in ("salePriceU", "salePrice", "finalPriceU", "finalPrice"):
            normalized = normalize_price(product.get(key))
            if normalized is not None:
                candidates.append(normalized)

        return min(candidates) if candidates else None

    @staticmethod
    def _extract_card_old_price(product: dict[str, Any], current_price: float | None) -> float | None:
        candidates: list[float] = []

        sizes = product.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                price_block = size.get("price")
                if isinstance(price_block, dict):
                    normalized = normalize_price(price_block.get("basic"))
                    if normalized is not None:
                        candidates.append(normalized)

        for key in ("priceU", "price", "basicPriceU", "basicPrice", "oldPriceU", "oldPrice"):
            normalized = normalize_price(product.get(key))
            if normalized is not None:
                candidates.append(normalized)

        if not candidates:
            return None

        old_price = max(candidates)
        if current_price is not None and old_price <= current_price:
            return None
        return old_price

    @classmethod
    def _extract_card_stock_qty(cls, product: dict[str, Any]) -> int | None:
        total_quantity = cls._to_int(product.get("totalQuantity"))
        if total_quantity is not None:
            return max(total_quantity, 0)

        quantities: list[int] = []
        sizes = product.get("sizes")
        if isinstance(sizes, list):
            for size in sizes:
                if not isinstance(size, dict):
                    continue
                stocks = size.get("stocks")
                if not isinstance(stocks, list):
                    continue
                for stock in stocks:
                    if not isinstance(stock, dict):
                        continue
                    qty = cls._to_int(
                        stock.get("qty")
                        or stock.get("quantity")
                        or stock.get("stock")
                        or stock.get("balance")
                    )
                    if qty is not None:
                        quantities.append(qty)

        if quantities:
            return sum(max(qty, 0) for qty in quantities)
        return None

    @staticmethod
    def _extract_card_in_stock_flag(product: dict[str, Any]) -> bool | None:
        for key in ("inStock", "available", "isAvailable"):
            value = product.get(key)
            if isinstance(value, bool):
                return value
        return None

    async def _respect_rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            elapsed = now - self._last_request_ts
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_ts = time.monotonic()

    async def _respect_card_rate_limit(self) -> None:
        async with self._card_rate_lock:
            now = time.monotonic()
            elapsed = now - self._card_last_request_ts
            if elapsed < self._card_min_interval:
                await asyncio.sleep(self._card_min_interval - elapsed)
            self._card_last_request_ts = time.monotonic()
