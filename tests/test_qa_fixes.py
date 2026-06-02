"""Regression-тесты по итогам полного QA-прохода (8 фиксов).

Хендлеры извлекаются из роутера по имени и вызываются напрямую с фейками
(паттерн как в test_agent_chat_handler) — без реального Telegram/сети.

Покрытие:
  #1 SettingsRepository.set_float + /settax            (был AttributeError)
  #2 /insights рендерит аномалию                        (был AttributeError severity/message)
  #3 purprompt:skip не перетирает оплаченную закупку
  #4 /arb_scan_now экранирует имя категории (safe_md)
  #5 фолбэк кнопки «🎯 Арбитраж» при выключенном арбитраже
  #7 /setspp отвергает nan/inf
  #8 briefing-loop переживает ошибку БД
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app import scheduler as sched_mod
from app.arbitrage import handlers as arbitrage_handlers
from app.handlers import business, main_menu, margin, purchase_prompts
from app.security import sign_payload
from app.storage.db import Database
from app.storage.repositories import SettingsRepository

pytestmark = pytest.mark.asyncio

SECRET = "qa-secret"


# ── helpers ──────────────────────────────────────────────────────────

def _cfg(**overrides: Any) -> Any:
    base: dict[str, Any] = dict(
        callback_signing_secret=SECRET,
        shadow_mode=False,
        is_user_allowed=lambda uid: True,
        arbitrage_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _msg(text: str = "x", chat_id: int = 1, uid: int = 1) -> Any:
    m = MagicMock()
    m.text = text
    m.chat = SimpleNamespace(id=chat_id, type="private")
    m.from_user = SimpleNamespace(id=uid)
    m.bot = AsyncMock()
    m.answer = AsyncMock()
    return m


def _cmd(args: str | None) -> Any:
    return SimpleNamespace(args=args)


def _cb(data: str) -> Any:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.answer = AsyncMock()
    return cb


def _handler(router: Any, name: str) -> Any:
    for collection in (router.message.handlers, router.callback_query.handlers):
        for h in collection:
            if h.callback.__name__ == name:
                return h.callback
    raise KeyError(name)


class FakeState:
    def __init__(self, data: dict | None = None, state: Any = None) -> None:
        self._data = dict(data or {})
        self._state = state

    async def get_data(self) -> dict:
        return dict(self._data)

    async def update_data(self, **kw: Any) -> dict:
        self._data.update(kw)
        return dict(self._data)

    async def set_state(self, state: Any = None) -> None:
        self._state = state

    async def clear(self) -> None:
        self._data = {}
        self._state = None


class FakeSettings:
    """Мини-SettingsRepository поверх dict (значения хранятся как в БД — строкой)."""

    def __init__(self) -> None:
        self.floats: dict[str, float] = {}
        self.values: dict[str, str] = {}

    async def set_float(self, key: str, value: float) -> None:
        self.floats[key] = value

    async def set_value(self, key: str, value: str) -> None:
        self.values[key] = value

    async def get_value(self, key: str) -> str | None:
        return self.values.get(key)

    async def get_float(self, key: str, default: float) -> float:
        if key in self.floats:
            return self.floats[key]
        raw = self.values.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            return default


class FakeStockRepo:
    def __init__(self, status: str) -> None:
        self._status = status
        self.resolved: list[tuple[int, str]] = []

    async def get_prompt(self, prompt_id: int) -> dict:
        return {"status": self._status, "nm_id": 1,
                "supplier_article": "ART", "qty_delta": 3}

    async def resolve(self, prompt_id: int, status: str,
                      purchase_id: int | None = None, note: str | None = None) -> None:
        self.resolved.append((prompt_id, status))


def _business_router(*, settings: Any = None, insight_engine: Any = None) -> Any:
    return business.get_router(
        config=_cfg(),
        business_repository=MagicMock(),
        settings_repository=settings or FakeSettings(),
        subscriber_repository=AsyncMock(),
        insight_engine=insight_engine or MagicMock(),
        updater=MagicMock(),
    )


def _margin_router(*, settings: Any = None) -> Any:
    return margin.get_router(
        config=_cfg(),
        item_repository=MagicMock(),
        meta_repository=MagicMock(),
        settings_repository=settings or FakeSettings(),
        subscriber_repository=AsyncMock(),
        tracked_article_repository=MagicMock(),
        updater=MagicMock(),
    )


# ── #1: SettingsRepository.set_float ─────────────────────────────────

async def test_settings_set_float_round_trip(tmp_path) -> None:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    repo = SettingsRepository(db)
    await repo.set_float("profit_tax_percent", 2.5)
    assert await repo.get_float("profit_tax_percent", 0.0) == 2.5
    await db.close()


async def test_settax_persists_via_set_float() -> None:
    settings = FakeSettings()
    handler = _handler(_business_router(settings=settings), "settax_handler")
    msg = _msg()
    await handler(msg, _cmd("2"))           # до фикса: AttributeError (set_float отсутствовал)
    assert settings.floats.get("profit_tax_percent") == 2.0
    assert "✅" in msg.answer.call_args.args[0]


# ── #2: /insights рендерит аномалию без падения ──────────────────────

async def test_insights_renders_anomaly_without_crash() -> None:
    from app.services.insight_engine import Insight

    ie = MagicMock()
    ie.detect_anomalies = AsyncMock(return_value=[
        Insight(level="warning", emoji="🟡", title="Сток на нуле",
                body="Деталь аномалии", action="Завезти товар"),
    ])
    handler = _handler(_business_router(insight_engine=ie), "insights_handler")
    msg = _msg()
    await handler(msg)                      # до фикса: AttributeError (ins.severity/ins.message)
    text = msg.answer.call_args.args[0]
    assert "Сток на нуле" in text
    assert "Деталь аномалии" in text


# ── #3: purprompt:skip не перетирает уже обработанную закупку ────────

async def test_purprompt_skip_guards_replied() -> None:
    repo = FakeStockRepo(status="replied")          # закупка уже проведена
    router = purchase_prompts.get_router(
        config=_cfg(), stock_arrival_repo=repo,
        business_repository=MagicMock(), subscriber_repository=AsyncMock(),
    )
    handler = _handler(router, "purchase_prompt_callback")
    await handler(_cb(sign_payload("purprompt:skip:5", SECRET)), FakeState())
    assert repo.resolved == []                       # НЕ перезаписал статус


async def test_purprompt_skip_resolves_pending() -> None:
    repo = FakeStockRepo(status="pending")
    router = purchase_prompts.get_router(
        config=_cfg(), stock_arrival_repo=repo,
        business_repository=MagicMock(), subscriber_repository=AsyncMock(),
    )
    handler = _handler(router, "purchase_prompt_callback")
    await handler(_cb(sign_payload("purprompt:skip:5", SECRET)), FakeState())
    assert repo.resolved == [(5, "ignored")]


# ── #4: /arb_scan_now экранирует имя категории под Markdown ──────────

async def test_arb_scan_now_escapes_category_name() -> None:
    scanner = MagicMock()
    scanner.scan_once = AsyncMock(return_value={"queries": 1, "candidates": 0, "alerted": 0})
    arb_repo = MagicMock()
    arb_repo.list_queries = AsyncMock(return_value=[
        {"query": "робот", "subject_name": "Кабель_USB*C", "subject_id": 1, "last_found_count": 5},
    ])
    arb_repo.get_category_avg_spp = AsyncMock(return_value=None)   # → попадёт в "нужно 3+ наблюдения"
    router = arbitrage_handlers.get_router(
        config=_cfg(), arb_repo=arb_repo, scanner=scanner,
        subscriber_repo=AsyncMock(), auto_observer=MagicMock(),
    )
    handler = _handler(router, "arb_scan_now")
    msg = _msg()
    await handler(msg)
    final = msg.answer.call_args_list[-1]
    text = final.args[0]
    assert final.kwargs.get("parse_mode") == "Markdown"
    assert "Кабель_USB*C" not in text        # сырое имя НЕ должно протечь (иначе Markdown-crash)
    assert "КабельUSBC" in text              # safe_md вырезал _ и *


# ── #5: фолбэк кнопки «🎯 Арбитраж» при выключенном арбитраже ────────

async def test_arbitrage_button_fallback_when_disabled() -> None:
    router = main_menu.get_router(config=_cfg(arbitrage_enabled=False), subscriber_repo=AsyncMock())
    handler = _handler(router, "arbitrage_disabled")
    msg = _msg("🎯 Арбитраж")
    await handler(msg)
    assert "выключен" in msg.answer.call_args.args[0].lower()


async def test_arbitrage_button_not_registered_when_enabled() -> None:
    router = main_menu.get_router(config=_cfg(arbitrage_enabled=True), subscriber_repo=AsyncMock())
    with pytest.raises(KeyError):
        _handler(router, "arbitrage_disabled")       # при включённом — кнопку ловит роутер арбитража


# ── #7: /setspp отвергает nan/inf ────────────────────────────────────

async def test_setspp_rejects_nan() -> None:
    settings = FakeSettings()
    handler = _handler(_margin_router(settings=settings), "set_spp_handler")
    msg = _msg()
    await handler(msg, _cmd("nan"))
    assert "spp_percent" not in settings.values       # nan НЕ сохранён
    assert "Введите число" in msg.answer.call_args.args[0]


async def test_setspp_rejects_inf() -> None:
    settings = FakeSettings()
    handler = _handler(_margin_router(settings=settings), "set_spp_handler")
    await handler(_msg(), _cmd("inf"))
    assert "spp_percent" not in settings.values


async def test_setspp_accepts_valid() -> None:
    settings = FakeSettings()
    handler = _handler(_margin_router(settings=settings), "set_spp_handler")
    await handler(_msg(), _cmd("24"))
    assert settings.values.get("spp_percent") == "24.0"


# ── #8: briefing-loop переживает ошибку БД, не умирая навсегда ───────

async def test_briefing_loop_survives_db_error(monkeypatch) -> None:
    stop = asyncio.Event()
    calls = {"n": 0}

    async def boom(key: str) -> Any:
        calls["n"] += 1
        if calls["n"] >= 3:
            stop.set()        # на 3-й итерации даём циклу выйти
            return None
        raise RuntimeError("db locked")

    now_msk = datetime.now(sched_mod.MSK)
    fake = SimpleNamespace(
        _stop_event=stop,
        _config=SimpleNamespace(briefing_hour=now_msk.hour, briefing_minute=0),
        _meta_repository=SimpleNamespace(get_value=boom, set_value=AsyncMock()),
        _send_briefing=AsyncMock(),
        _logger=MagicMock(),
        _last_briefing_date=None,
    )

    async def instant_timeout(coro: Any, timeout: float) -> Any:
        coro.close()                       # избегаем "coroutine never awaited"
        raise asyncio.TimeoutError

    monkeypatch.setattr(sched_mod.asyncio, "wait_for", instant_timeout)

    # До фикта первая же ошибка boom() пробросилась бы наружу из except TimeoutError
    # и убила бы цикл. С фиксом — ловится, цикл доживает до stop.
    await sched_mod.WbUpdateScheduler._run_briefing_loop(fake)

    assert calls["n"] >= 3                  # пережил две ошибки подряд
    assert fake._logger.exception.called    # и залогировал их
