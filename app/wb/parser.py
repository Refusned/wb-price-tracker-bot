from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from app.storage.models import Item

logger = logging.getLogger(__name__)

_PRICE_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:[\.,]\d+)?")


def normalize_price(value: Any) -> float | None:
    """
    Normalize WB price value into RUB.

    WB often returns integers in kopecks (e.g. `999000` for 9 990 RUB).
    Heuristic: if value > 100000, treat as kopecks and divide by 100.
    """
    if value is None or isinstance(value, bool):
        return None

    numeric: float | None = None
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        match = _PRICE_NUMBER_PATTERN.search(value.replace(" ", ""))
        if not match:
            return None
        numeric = float(match.group(0).replace(",", "."))

    if numeric is None or numeric <= 0:
        return None

    if numeric > 100000:
        numeric = numeric / 100

    return round(numeric, 2)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
            return int(value)
    return None


def _extract_nm_id(product: dict[str, Any]) -> str | None:
    for key in ("nmId", "nmID", "id", "sku"):
        raw = product.get(key)
        if raw is None:
            continue
        text = str(raw).strip()
        if text:
            return text
    return None


def _extract_name(product: dict[str, Any], nm_id: str) -> str:
    name = product.get("name") or product.get("title")
    if isinstance(name, str) and name.strip():
        return name.strip()

    brand = product.get("brand")
    if isinstance(brand, str) and brand.strip():
        return f"{brand.strip()} {nm_id}".strip()

    return f"WB товар {nm_id}"


def _extract_price(product: dict[str, Any]) -> float | None:
    direct_keys = (
        "salePriceU",
        "salePrice",
        "discountedPriceU",
        "discountedPrice",
        "finalPriceU",
        "finalPrice",
        "priceU",
        "price",
    )
    for key in direct_keys:
        normalized = normalize_price(product.get(key))
        if normalized is not None:
            return normalized

    sizes = product.get("sizes")
    if isinstance(sizes, list):
        for size in sizes:
            if not isinstance(size, dict):
                continue
            price_block = size.get("price")
            if isinstance(price_block, dict):
                for key in ("total", "product", "sale"):
                    normalized = normalize_price(price_block.get(key))
                    if normalized is not None:
                        return normalized
            normalized = normalize_price(size.get("price"))
            if normalized is not None:
                return normalized

    return None


def _extract_old_price(product: dict[str, Any], current_price: float | None) -> float | None:
    direct_keys = (
        "priceU",
        "price",
        "basicPriceU",
        "basicPrice",
        "oldPriceU",
        "oldPrice",
    )
    for key in direct_keys:
        normalized = normalize_price(product.get(key))
        if normalized is None:
            continue
        if current_price is not None and normalized <= current_price:
            continue
        return normalized

    return None


def _iter_stock_blocks(product: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("stocks", "stock", "skus"):
        block = product.get(key)
        if isinstance(block, list):
            for row in block:
                if isinstance(row, dict):
                    yield row
        elif isinstance(block, dict):
            yield block

    sizes = product.get("sizes")
    if isinstance(sizes, list):
        for size in sizes:
            if not isinstance(size, dict):
                continue
            nested = size.get("stocks") or size.get("stock")
            if isinstance(nested, list):
                for row in nested:
                    if isinstance(row, dict):
                        yield row
            elif isinstance(nested, dict):
                yield nested


def _extract_stock_info(product: dict[str, Any]) -> tuple[bool, int | None]:
    qty_candidates = ("totalQuantity", "quantity", "qty", "stock", "stocksQty")

    quantities: list[int] = []
    for key in qty_candidates:
        qty = _as_int(product.get(key))
        if qty is not None:
            quantities.append(qty)

    for block in _iter_stock_blocks(product):
        for key in ("quantity", "qty", "balance", "stock", "amount"):
            qty = _as_int(block.get(key))
            if qty is not None:
                quantities.append(qty)
                break

    if quantities:
        total = sum(max(qty, 0) for qty in quantities)
        return total > 0, total

    boolean_flags = []
    for key in ("inStock", "available", "isAvailable"):
        raw = product.get(key)
        if isinstance(raw, bool):
            boolean_flags.append(raw)

    if True in boolean_flags:
        return True, None

    return False, None


def _extract_products(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    candidates = [
        payload.get("products"),
        payload.get("data", {}).get("products") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("cards") if isinstance(payload.get("data"), dict) else None,
        payload.get("data", {}).get("items") if isinstance(payload.get("data"), dict) else None,
        payload.get("cards"),
        payload.get("items"),
    ]

    for candidate in candidates:
        if isinstance(candidate, list):
            return [row for row in candidate if isinstance(row, dict)]

    # Fallback for new WB payload shapes: recursively collect dict rows that look like products.
    # This keeps parser resilient when WB moves product arrays to another nesting key.
    seen_ids: set[str] = set()
    collected: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            maybe_nm_id = _extract_nm_id(node)
            if maybe_nm_id and _extract_price(node) is not None and maybe_nm_id not in seen_ids:
                seen_ids.add(maybe_nm_id)
                collected.append(node)

            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)

    walk(payload)
    if collected:
        return collected

    return []


def parse_products(payload: Any) -> list[Item]:
    products = _extract_products(payload)
    parsed: list[Item] = []

    for product in products:
        try:
            nm_id = _extract_nm_id(product)
            if not nm_id:
                continue

            price_rub = _extract_price(product)
            if price_rub is None:
                continue

            old_price_rub = _extract_old_price(product, price_rub)
            in_stock, stock_qty = _extract_stock_info(product)
            name = _extract_name(product, nm_id)
            url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"

            parsed.append(
                Item(
                    nm_id=nm_id,
                    name=name,
                    price_rub=price_rub,
                    old_price_rub=old_price_rub,
                    in_stock=in_stock,
                    stock_qty=stock_qty,
                    url=url,
                )
            )
        except Exception as exc:  # defensive: skip malformed item instead of failing whole update
            logger.debug("Failed to parse WB product: %s", exc)

    return parsed


def filter_items_for_top(items: list[Item], min_price_rub: int, limit: int = 10) -> list[Item]:
    filtered = [item for item in items if item.in_stock and item.price_rub >= float(min_price_rub)]
    filtered.sort(key=lambda item: item.price_rub)
    return filtered[:limit]
