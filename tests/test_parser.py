from app.wb.parser import normalize_price, parse_products


def test_parse_products_extracts_required_fields() -> None:
    payload = {
        "data": {
            "products": [
                {
                    "nmId": 123456789,
                    "name": "Умная колонка",
                    "salePriceU": 999000,
                    "priceU": 1199000,
                    "totalQuantity": 12,
                },
                {
                    "nmId": 987654321,
                    "name": "Товар без остатков",
                    "salePrice": 10500,
                },
            ]
        }
    }

    items = parse_products(payload)
    assert len(items) == 2

    first = items[0]
    assert first.nm_id == "123456789"
    assert first.name == "Умная колонка"
    assert first.price_rub == 9990.0
    assert first.old_price_rub == 11990.0
    assert first.in_stock is True
    assert first.stock_qty == 12
    assert first.url.endswith("/123456789/detail.aspx")

    second = items[1]
    assert second.nm_id == "987654321"
    assert second.in_stock is False
    assert second.stock_qty is None


def test_normalize_price_kopecks_and_rubles() -> None:
    assert normalize_price(999000) == 9990.0
    assert normalize_price(9990) == 9990.0
    assert normalize_price("11 990") == 11990.0


def test_parse_products_recursively_finds_nested_cards() -> None:
    payload = {
        "metadata": {"catalog_type": "preset", "catalog_value": "preset=123"},
        "search_result": {
            "nested": {
                "cards": [
                    {
                        "nmId": 111,
                        "name": "Умная колонка 111",
                        "salePriceU": 1234000,
                        "inStock": True,
                    }
                ]
            }
        },
    }

    items = parse_products(payload)
    assert len(items) == 1
    assert items[0].nm_id == "111"
    assert items[0].price_rub == 12340.0
    assert items[0].in_stock is True
