"""Тесты хендлеров режима «🤖 Ассистент» (agent_chat).

Хендлеры извлекаются из роутера по имени и вызываются напрямую с фейками
(Message/CallbackQuery/FSMContext) — без реального Telegram. Плюс модульная
execute_action (роутинг мутаций, shadow_mode) и фильтр сосуществования с командами.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import F

from app.handlers import agent_chat
from app.handlers.agent_chat import (
    AGENT_BUTTON,
    NEW_BTN,
    STOP_BTN,
    AgentChatStates,
    execute_action,
)
from app.security import sign_payload
from app.services.cabinet_agent import AgentTurn, ProposedAction

pytestmark = pytest.mark.asyncio

SECRET = "s3cret"


class FakeState:
    """Мини-FSMContext поверх dict."""

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


class _NullCtx:
    async def __aenter__(self) -> "_NullCtx":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeAgent:
    def __init__(self, turns: list[AgentTurn]) -> None:
        self._turns = list(turns)
        self.run_calls: list[tuple[int, str]] = []
        self.reset_calls: list[int] = []

    async def run_turn(self, chat_id: int, text: str) -> AgentTurn:
        self.run_calls.append((chat_id, text))
        return self._turns.pop(0) if self._turns else AgentTurn("(пусто)", [])

    async def reset(self, chat_id: int) -> None:
        self.reset_calls.append(chat_id)


class FakeBiz:
    def __init__(self) -> None:
        self.purchases: list[tuple] = []

    async def add_purchase(self, *, nm_id, supplier_article, quantity,
                           buy_price_per_unit, spp_at_purchase, notes) -> int:
        self.purchases.append((nm_id, supplier_article, quantity, buy_price_per_unit, notes))
        return 7


class FakeSettings:
    def __init__(self) -> None:
        self.sets: list[tuple[str, str]] = []

    async def set_value(self, key: str, value: str) -> None:
        self.sets.append((key, value))


class FakeFb:
    def __init__(self) -> None:
        self.answered: list[tuple] = []

    async def answer_feedback(self, fid: str, text: str) -> None:
        self.answered.append(("feedback", fid, text))

    async def answer_question(self, qid: str, text: str) -> None:
        self.answered.append(("question", qid, text))


class FakeReply:
    def __init__(self, handled: bool = False) -> None:
        self._handled = handled
        self.records: list[dict] = []

    async def is_handled(self, kind: str, fid: str) -> bool:
        return self._handled

    async def record(self, **kw: Any) -> None:
        self.records.append(kw)


def _cfg(*, shadow: bool = False) -> Any:
    return SimpleNamespace(
        callback_signing_secret=SECRET, shadow_mode=shadow, feedback_signature="",
        is_user_allowed=lambda uid: True,
    )


def _router(agent: FakeAgent, *, cfg: Any | None = None, biz=None, settings=None, fb=None, repo=None):
    return agent_chat.get_router(
        config=cfg or _cfg(),
        cabinet_agent=agent,  # type: ignore[arg-type]
        subscriber_repository=AsyncMock(),
        business_repository=biz or FakeBiz(),  # type: ignore[arg-type]
        settings_repository=settings or FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=fb,
        reply_repo=repo,
    )


def _handler(router: Any, name: str):
    for collection in (router.message.handlers, router.callback_query.handlers):
        for h in collection:
            if h.callback.__name__ == name:
                return h.callback
    raise KeyError(name)


def _msg(text: str = "привет", chat_id: int = 1) -> Any:
    m = MagicMock()
    m.text = text
    m.chat = SimpleNamespace(id=chat_id, type="private")
    m.from_user = SimpleNamespace(id=999)
    m.bot = AsyncMock()
    m.answer = AsyncMock()
    return m


def _cb(data: str) -> Any:
    cb = MagicMock()
    cb.data = data
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    return cb


# ---------- enter / exit / new ----------

async def test_enter_sets_active_state() -> None:
    router = _router(FakeAgent([]))
    enter = _handler(router, "enter_button")
    state = FakeState()
    await enter(_msg(AGENT_BUTTON), state)
    assert state._state == AgentChatStates.Active
    assert state._data["pending"] == {} and state._data["next_pid"] == 0
    assert state._data["busy"] is False


async def test_stop_clears_state() -> None:
    router = _router(FakeAgent([]))
    stop = _handler(router, "stop_button")
    state = FakeState(data={"pending": {"0": {}}}, state=AgentChatStates.Active)
    msg = _msg(STOP_BTN)
    await stop(msg, state)
    assert state._state is None and state._data == {}
    msg.answer.assert_awaited_once()


async def test_new_dialog_resets_history() -> None:
    agent = FakeAgent([])
    router = _router(agent)
    new = _handler(router, "new_dialog")
    state = FakeState(data={"pending": {"0": {}}, "next_pid": 3}, state=AgentChatStates.Active)
    await new(_msg(NEW_BTN), state)
    assert agent.reset_calls == [1]
    assert state._data["pending"] == {} and state._data["next_pid"] == 0


# ---------- dialog turn ----------

async def test_turn_renders_proposals_with_monotonic_pid(monkeypatch: Any) -> None:
    monkeypatch.setattr(agent_chat.ChatActionSender, "typing", lambda **kw: _NullCtx())
    agent = FakeAgent([
        AgentTurn("ответ1", [ProposedAction("purchase", {"a": 1}, "закупка A")]),
        AgentTurn("ответ2", [ProposedAction("purchase", {"a": 2}, "закупка B")]),
    ])
    router = _router(agent)
    turn = _handler(router, "dialog_turn")
    state = FakeState(data={"pending": {}, "next_pid": 0, "busy": False},
                      state=AgentChatStates.Active)

    await turn(_msg("что докупить?"), state)
    await turn(_msg("а ещё?"), state)

    # два хода → два РАЗНЫХ pid, без перезаписи (фикс коллизии callback_data)
    assert set(state._data["pending"].keys()) == {"0", "1"}
    assert state._data["next_pid"] == 2
    assert len(agent.run_calls) == 2


async def test_turn_button_callback_data_signed(monkeypatch: Any) -> None:
    monkeypatch.setattr(agent_chat.ChatActionSender, "typing", lambda **kw: _NullCtx())
    agent = FakeAgent([AgentTurn("ответ", [ProposedAction("purchase", {"a": 1}, "закупка A")])])
    router = _router(agent)
    turn = _handler(router, "dialog_turn")
    msg = _msg("докупить?")
    state = FakeState(data={"pending": {}, "next_pid": 0, "busy": False},
                      state=AgentChatStates.Active)
    await turn(msg, state)
    # найти сообщение с inline-клавиатурой
    kb_calls = [c for c in msg.answer.call_args_list if c.kwargs.get("reply_markup")]
    assert kb_calls, "ожидалась inline-кнопка подтверждения"
    btn = kb_calls[0].kwargs["reply_markup"].inline_keyboard[0][0]
    assert btn.callback_data == sign_payload("agent:do:0", SECRET)


async def test_busy_guard_blocks_second_turn() -> None:
    agent = FakeAgent([AgentTurn("x", [])])
    router = _router(agent)
    turn = _handler(router, "dialog_turn")
    state = FakeState(data={"pending": {}, "next_pid": 0, "busy": True},
                      state=AgentChatStates.Active)
    msg = _msg("вопрос")
    await turn(msg, state)
    assert agent.run_calls == []  # run_turn НЕ запущен повторно
    msg.answer.assert_awaited_once()


# ---------- confirm callback ----------

def _pending_purchase() -> dict:
    return {"0": {"kind": "purchase",
                  "params": {"nm_id": 111, "supplier_article": "ART-1",
                             "quantity": 10, "buy_price_per_unit": 500.0, "notes": None},
                  "summary": "Записать закупку"}}


async def test_callback_valid_executes_purchase() -> None:
    biz = FakeBiz()
    router = _router(FakeAgent([]), biz=biz)
    cb_handler = _handler(router, "agent_callback")
    state = FakeState(data={"pending": _pending_purchase()}, state=AgentChatStates.Active)
    cb = _cb(sign_payload("agent:do:0", SECRET))
    await cb_handler(cb, state)
    assert biz.purchases == [(111, "ART-1", 10, 500.0, None)]
    assert state._data["pending"] == {}  # пендинг одноразовый
    cb.message.edit_text.assert_awaited_once()


async def test_callback_tampered_signature_does_not_execute() -> None:
    biz = FakeBiz()
    router = _router(FakeAgent([]), biz=biz)
    cb_handler = _handler(router, "agent_callback")
    state = FakeState(data={"pending": _pending_purchase()}, state=AgentChatStates.Active)
    cb = _cb("agent:do:0:badtag")
    await cb_handler(cb, state)
    assert biz.purchases == []  # подпись не прошла → НЕ исполнили
    cb.answer.assert_awaited()  # показали alert


async def test_callback_stale_pid() -> None:
    biz = FakeBiz()
    router = _router(FakeAgent([]), biz=biz)
    cb_handler = _handler(router, "agent_callback")
    state = FakeState(data={"pending": {}}, state=AgentChatStates.Active)
    cb = _cb(sign_payload("agent:do:5", SECRET))
    await cb_handler(cb, state)
    assert biz.purchases == []


async def test_callback_cancel_removes_pending() -> None:
    biz = FakeBiz()
    router = _router(FakeAgent([]), biz=biz)
    cb_handler = _handler(router, "agent_callback")
    state = FakeState(data={"pending": _pending_purchase()}, state=AgentChatStates.Active)
    cb = _cb(sign_payload("agent:cancel:0", SECRET))
    await cb_handler(cb, state)
    assert biz.purchases == []
    assert state._data["pending"] == {}


# ---------- execute_action (модульная, money-safety) ----------

async def test_execute_purchase() -> None:
    biz = FakeBiz()
    out = await execute_action(
        {"kind": "purchase", "params": {"nm_id": 111, "supplier_article": "ART-1",
                                        "quantity": 10, "buy_price_per_unit": 500.0, "notes": None}},
        business_repository=biz, settings_repository=FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=None, reply_repo=None, config=_cfg())
    assert "✅" in out and biz.purchases == [(111, "ART-1", 10, 500.0, None)]


async def test_execute_purchase_bad_amount() -> None:
    biz = FakeBiz()
    out = await execute_action(
        {"kind": "purchase", "params": {"nm_id": 111, "quantity": 0, "buy_price_per_unit": 500.0}},
        business_repository=biz, settings_repository=FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=None, reply_repo=None, config=_cfg())
    assert "❌" in out and biz.purchases == []


async def test_execute_profit_setting() -> None:
    settings = FakeSettings()
    out = await execute_action(
        {"kind": "profit_setting", "params": {"param": "tax",
                                              "settings_key": "profit_tax_percent", "value": 3.0}},
        business_repository=FakeBiz(), settings_repository=settings,  # type: ignore[arg-type]
        feedbacks_client=None, reply_repo=None, config=_cfg())
    assert "✅" in out and settings.sets == [("profit_tax_percent", "3.0")]


async def test_execute_profit_setting_bad_key() -> None:
    settings = FakeSettings()
    out = await execute_action(
        {"kind": "profit_setting", "params": {"settings_key": "evil_key", "value": 3.0}},
        business_repository=FakeBiz(), settings_repository=settings,  # type: ignore[arg-type]
        feedbacks_client=None, reply_repo=None, config=_cfg())
    assert "❌" in out and settings.sets == []


async def test_execute_feedback_posts_even_in_shadow() -> None:
    # Решение: ручное подтверждение публикует НЕЗАВИСИМО от shadow_mode (защита —
    # подпись кнопки + контент-гейт + идемпотентность + только владелец).
    fb = FakeFb()
    repo = FakeReply()
    out = await execute_action(
        {"kind": "feedback_reply", "params": {"target_id": "F1", "target_kind": "feedback",
                                              "text": "Спасибо за отзыв, рады что понравилось!"}},
        business_repository=FakeBiz(), settings_repository=FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=fb, reply_repo=repo, config=_cfg(shadow=True))
    assert "✅" in out and len(fb.answered) == 1


async def test_execute_feedback_posts_when_not_shadow() -> None:
    fb = FakeFb()
    repo = FakeReply(handled=False)
    out = await execute_action(
        {"kind": "feedback_reply", "params": {"target_id": "F1", "target_kind": "feedback",
                                              "text": "Спасибо за отзыв, рады что понравилось!"}},
        business_repository=FakeBiz(), settings_repository=FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=fb, reply_repo=repo, config=_cfg(shadow=False))
    assert "✅" in out
    assert len(fb.answered) == 1 and fb.answered[0][0] == "feedback"
    # идемпотентность: резерв pending + подтверждение posted
    assert [r["status"] for r in repo.records] == ["pending", "posted"]


async def test_execute_feedback_content_gate_blocks_phone() -> None:
    fb = FakeFb()
    repo = FakeReply()
    out = await execute_action(
        {"kind": "feedback_reply", "params": {"target_id": "F1", "target_kind": "feedback",
                                              "text": "Звоните +7 900 123 45 67"}},
        business_repository=FakeBiz(), settings_repository=FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=fb, reply_repo=repo, config=_cfg(shadow=False))
    assert "❌" in out and fb.answered == []  # телефон не прошёл гейт


async def test_execute_feedback_already_handled() -> None:
    fb = FakeFb()
    repo = FakeReply(handled=True)
    out = await execute_action(
        {"kind": "feedback_reply", "params": {"target_id": "F1", "target_kind": "feedback",
                                              "text": "Спасибо большое за отзыв!"}},
        business_repository=FakeBiz(), settings_repository=FakeSettings(),  # type: ignore[arg-type]
        feedbacks_client=fb, reply_repo=repo, config=_cfg(shadow=False))
    assert "уже" in out.lower() and fb.answered == []


# ---------- фильтр сосуществования с командами ----------

async def test_dialog_filter_excludes_commands_and_mode_buttons() -> None:
    flt = F.text & ~F.text.startswith("/") & ~F.text.in_({STOP_BTN, NEW_BTN, AGENT_BUTTON})
    assert bool(flt.resolve(SimpleNamespace(text="почему упали продажи"))) is True
    assert bool(flt.resolve(SimpleNamespace(text="/stock"))) is False
    assert bool(flt.resolve(SimpleNamespace(text="/advice"))) is False
    assert bool(flt.resolve(SimpleNamespace(text=STOP_BTN))) is False
    assert bool(flt.resolve(SimpleNamespace(text=AGENT_BUTTON))) is False
