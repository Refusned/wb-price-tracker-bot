from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Item:
    nm_id: str
    name: str
    price_rub: float
    old_price_rub: float | None
    in_stock: bool
    stock_qty: int | None
    url: str


@dataclass(slots=True)
class PriceDropEvent:
    nm_id: str
    name: str
    url: str
    previous_price_rub: float
    new_price_rub: float
    drop_percent: float
    stock_qty: int | None
    top_rank: int | None = None


def item_from_row(row: dict) -> Item:
    return Item(
        nm_id=str(row["nm_id"]),
        name=str(row["name"]),
        price_rub=float(row["price_rub"]),
        old_price_rub=float(row["old_price_rub"]) if row["old_price_rub"] is not None else None,
        in_stock=bool(row["in_stock"]),
        stock_qty=int(row["stock_qty"]) if row["stock_qty"] is not None else None,
        url=str(row["url"]),
    )
