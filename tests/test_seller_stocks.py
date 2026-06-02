"""Юнит-тесты SellerClient.get_stocks / get_all_fbs_stocks.

Только фейк-сессия — НИКАКИХ реальных вызовов WB (money-safety): мутирующих
эндпоинтов здесь нет, но даже GET/POST бьют в фейк, не в *.wildberries.ru.
"""
from __future__ import annotations

import asyncio
import json as _json
from typing import Any

import aiohttp
import pytest

from app.wb.seller_client import SellerApiError, SellerClient

pytestmark = pytest.mark.asyncio


async def _nosleep(*_a: Any, **_k: Any) -> None:
    return None


class _Resp:
    def __init__(self, status: int, payload: Any = None, *, raise_exc: Exception | None = None) -> None:
        self.status = status
        self._payload = payload
        self._raise_exc = raise_exc

    async def __aenter__(self) -> "_Resp":
        if self._raise_exc is not None:
            raise self._raise_exc
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def json(self) -> Any:
        return self._payload

    async def text(self) -> str:
        return _json.dumps(self._payload)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientError(f"HTTP {self.status}")


class _FakeSession:
    def __init__(self) -> None:
        self.get_q: list[_Resp] = []
        self.post_q: list[_Resp] = []
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, *, params: Any = None, headers: Any = None, timeout: Any = None) -> _Resp:
        self.calls.append({"method": "GET", "url": url, "params": params})
        return self.get_q.pop(0)

    def post(self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _Resp:
        self.calls.append({"method": "POST", "url": url, "json": json})
        return self.post_q.pop(0)


def _client(session: _FakeSession) -> SellerClient:
    return SellerClient(session, api_key="TOKEN", timeout_seconds=1.0)  # type: ignore[arg-type]


def _stock_row(nm_id: int, qty: int = 10) -> dict[str, Any]:
    return {
        "nmId": nm_id, "supplierArticle": f"A-{nm_id}", "warehouseName": "Коледино",
        "quantity": qty, "inWayToClient": 2, "inWayFromClient": 1,
        "quantityFull": qty + 3, "subject": "Колонка", "lastChangeDate": "2026-06-01T00:00:00",
    }


async def test_get_stocks_parses_entries_with_epoch_cursor() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(200, [_stock_row(1), _stock_row(2)]))
    entries = await _client(session).get_stocks()

    assert [e.nm_id for e in entries] == [1, 2]
    assert entries[0].quantity == 10
    assert entries[0].in_way_to_client == 2
    # Полный снимок: стартуем с эпохи 2019-06-20, а не с узкого окна "−N дней".
    assert session.calls[0]["params"]["dateFrom"].startswith("2019-06-20")


async def test_get_stocks_empty_is_empty_not_error() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(200, []))
    assert await _client(session).get_stocks() == []  # пусто = реально нет остатков


async def test_get_stocks_raises_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    session = _FakeSession()
    for _ in range(3):
        session.get_q.append(_Resp(0, raise_exc=aiohttp.ClientError("network")))
    with pytest.raises(SellerApiError):
        await _client(session).get_stocks()
    # ровно 3 попытки, потом сдаёмся с исключением (а не тихим [])
    assert sum(1 for c in session.calls if c["method"] == "GET") == 3


async def test_get_stocks_429_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _nosleep)
    session = _FakeSession()
    session.get_q.append(_Resp(429))
    session.get_q.append(_Resp(200, [_stock_row(5)]))
    entries = await _client(session).get_stocks()
    assert [e.nm_id for e in entries] == [5]


async def test_fbs_returns_ok_true_on_empty() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(200, []))  # warehouses — пусто
    session.post_q.append(_Resp(200, {"cards": [], "cursor": {}}))  # content cards — пусто
    entries, ok = await _client(session).get_all_fbs_stocks()
    assert entries == [] and ok is True  # реально пусто (нет FBS-склада) — пуржить можно


async def test_fbs_returns_ok_false_on_error() -> None:
    session = _FakeSession()
    session.get_q.append(_Resp(0, raise_exc=aiohttp.ClientError("net")))  # warehouses падает
    entries, ok = await _client(session).get_all_fbs_stocks()
    assert entries == [] and ok is False  # сетевой сбой → planner НЕ должен пуржить


async def test_get_stocks_truncation_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # Усечённый снимок (>= лимита WB) → SellerApiError, чтобы planner счёл FBO
    # неуспешным и НЕ пуржил валидный «хвост» за пределами лимита.
    monkeypatch.setattr("app.wb.seller_client._STOCKS_ROW_LIMIT", 2)
    session = _FakeSession()
    session.get_q.append(_Resp(200, [_stock_row(1), _stock_row(2)]))  # ровно лимит
    with pytest.raises(SellerApiError):
        await _client(session).get_stocks()


async def test_get_content_cards_raises_on_non_200() -> None:
    # Смена контракта break→raise: не-200 на карточках = неполный маппинг → raise.
    session = _FakeSession()
    session.post_q.append(_Resp(401, {"error": "unauthorized"}))
    with pytest.raises(SellerApiError):
        await _client(session).get_content_cards()


async def test_get_content_cards_raises_on_client_error() -> None:
    session = _FakeSession()
    session.post_q.append(_Resp(0, raise_exc=aiohttp.ClientError("net")))
    with pytest.raises(SellerApiError):
        await _client(session).get_content_cards()


async def test_fbs_ok_false_on_stock_fetch_error() -> None:
    # warehouses+cards успешны, но POST остатков склада падает в середине
    # агрегации → ([], False): снимок неполный, planner не пуржит.
    session = _FakeSession()
    session.get_q.append(_Resp(200, [{"id": 1, "name": "Москва"}]))  # warehouses
    session.post_q.append(_Resp(200, {"cards": [  # content cards
        {"nmID": 10, "vendorCode": "A-10", "subjectName": "X",
         "sizes": [{"skus": ["BARCODE1"]}]}
    ], "cursor": {}}))
    session.post_q.append(_Resp(0, raise_exc=aiohttp.ClientError("net")))  # get_fbs_stocks падает
    entries, ok = await _client(session).get_all_fbs_stocks()
    assert entries == [] and ok is False
