from app.storage.models import Item
from app.wb.client import WildberriesClient
from app.wb.parser import filter_items_for_top


def _item(nm_id: str, price: float, in_stock: bool, qty: int | None = None) -> Item:
    return Item(
        nm_id=nm_id,
        name=f"Item {nm_id}",
        price_rub=price,
        old_price_rub=None,
        in_stock=in_stock,
        stock_qty=qty,
        url=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
    )


def test_filter_items_for_top_min_price_stock_and_sort() -> None:
    items = [
        _item("1", 12000, True, 5),
        _item("2", 9500, True, 1),
        _item("3", 9000, False, 0),
        _item("4", 10000, True, None),
        _item("5", 8900, True, 2),
    ]

    top = filter_items_for_top(items, min_price_rub=9000, limit=10)

    assert [item.nm_id for item in top] == ["2", "4", "1"]
    assert all(item.in_stock for item in top)
    assert all(item.price_rub >= 9000 for item in top)


def test_relevant_name_filter_for_query_tokens() -> None:
    items = [
        _item("1", 11000, True),
        _item("2", 12000, True),
        _item("3", 13000, True),
    ]
    items[0].name = "Умная колонка ABC"
    items[1].name = "Колонка XYZ Pro"
    items[2].name = "Платье вечернее"

    filtered = WildberriesClient._filter_relevant_items(items, query="Умная колонка")
    assert {item.nm_id for item in filtered} == {"1"}


def test_relevant_name_filter_falls_back_to_any_token() -> None:
    items = [
        _item("1", 10000, True),
        _item("2", 12000, True),
    ]
    items[0].name = "Чайник электрический"
    items[1].name = "Утюг паровой"

    filtered = WildberriesClient._filter_relevant_items(items, query="чайник турка")
    assert [item.nm_id for item in filtered] == ["1"]
