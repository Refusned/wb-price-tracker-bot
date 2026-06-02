"""Тесты CabinetAgent (agent-loop). Никаких реальных LLM/WB — scripted FakeLLM
и FakeToolset. Проверяем: цикл исполняет инструменты и доходит до ответа; сбор
и дедуп предложений; лимит итераций → graceful финал; ошибка LLM не пишет
историю; история переживает «рестарт» (новый агент над тем же репозиторием).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.llm.client import ChatResult, LLMError, ToolCall
from app.services.cabinet_agent import CabinetAgent
from app.storage.db import Database
from app.storage.dialog_repository import DialogRepository

pytestmark = pytest.mark.asyncio


def _tc(name: str, args: dict) -> ToolCall:
    return ToolCall(name=name, arguments=args,
                    raw={"type": "function", "function": {"name": name, "arguments": args}})


def _tools_msg(tcs: list[ToolCall]) -> ChatResult:
    return ChatResult(content="", tool_calls=tcs,
                      raw_message={"role": "assistant", "content": "", "tool_calls": [t.raw for t in tcs]})


def _final(text: str) -> ChatResult:
    return ChatResult(content=text, tool_calls=[], raw_message={"role": "assistant", "content": text})


class FakeLLM:
    def __init__(self, script: list[ChatResult], raise_first: bool = False) -> None:
        self._script = list(script)
        self.raise_first = raise_first
        self.calls: list[dict[str, Any]] = []
        self.last_messages: list[dict[str, Any]] | None = None

    async def chat(self, messages, *, tools=None, temperature=0.3, num_predict=None, think=None) -> ChatResult:
        self.calls.append({"tools": tools, "think": think})
        self.last_messages = list(messages)
        if self.raise_first and len(self.calls) == 1:
            raise LLMError("llm down")
        return self._script.pop(0) if self._script else _final("(пусто)")


class FakeToolset:
    def __init__(self, outputs: dict[str, str] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[str, dict]] = []
        self.turns = 0

    def schemas(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "get_stock_summary"}}]

    def new_turn(self) -> None:
        self.turns += 1

    async def article_index(self) -> list[dict]:
        return [{"nm_id": 111, "supplier_article": "ART-1", "subject": "Куртка"}]

    async def call(self, name: str, args: dict) -> str:
        self.calls.append((name, args))
        return self.outputs.get(name, '{"ok": true}')


async def _agent(tmp_path: Path, llm: FakeLLM, tools: FakeToolset, **kw: Any) -> tuple[Database, CabinetAgent]:
    db = Database((tmp_path / "app.db").as_posix())
    await db.connect()
    await db.migrate()
    await db.apply_migrations()
    agent = CabinetAgent(llm_client=llm, toolset=tools,  # type: ignore[arg-type]
                         dialog_repo=DialogRepository(db), **kw)
    return db, agent


_PURCHASE_OUT = json.dumps({
    "ok": True, "kind": "purchase",
    "params": {"nm_id": 111, "supplier_article": "ART-1", "quantity": 10,
               "buy_price_per_unit": 500.0, "notes": None},
    "summary": "Записать закупку: 10 шт × 500 ₽ — ART-1",
}, ensure_ascii=False)


async def test_run_turn_executes_tool_then_answers(tmp_path: Path) -> None:
    llm = FakeLLM([_tools_msg([_tc("get_stock_summary", {})]), _final("Остатки в норме")])
    tools = FakeToolset({"get_stock_summary": '{"articles":[]}'})
    db, agent = await _agent(tmp_path, llm, tools)
    try:
        turn = await agent.run_turn(1, "как остатки?")
        assert turn.text == "Остатки в норме"
        assert ("get_stock_summary", {}) in tools.calls
        assert tools.turns == 1  # new_turn вызван
        assert llm.calls[0]["think"] is False  # дефолт think=False
        # история: user + assistant сохранены
        recent = await DialogRepository(db).get_recent(1)
        assert recent == [{"role": "user", "content": "как остатки?"},
                          {"role": "assistant", "content": "Остатки в норме"}]
    finally:
        await db.close()


async def test_collects_proposal(tmp_path: Path) -> None:
    llm = FakeLLM([_tools_msg([_tc("propose_purchase", {"nm_id": 111, "quantity": 10,
                                                        "buy_price_per_unit": 500})]),
                   _final("Предложил закупку — подтверди кнопкой")])
    tools = FakeToolset({"propose_purchase": _PURCHASE_OUT})
    db, agent = await _agent(tmp_path, llm, tools)
    try:
        turn = await agent.run_turn(1, "что докупить?")
        assert len(turn.proposals) == 1
        p = turn.proposals[0]
        assert p.kind == "purchase" and p.params["quantity"] == 10
        assert "закупк" in p.summary.lower()
    finally:
        await db.close()


async def test_dedup_proposals(tmp_path: Path) -> None:
    # модель предложила одно и то же дважды в разных раундах → одна кнопка
    llm = FakeLLM([
        _tools_msg([_tc("propose_purchase", {"nm_id": 111, "quantity": 10, "buy_price_per_unit": 500})]),
        _tools_msg([_tc("propose_purchase", {"nm_id": 111, "quantity": 10, "buy_price_per_unit": 500})]),
        _final("готово"),
    ])
    tools = FakeToolset({"propose_purchase": _PURCHASE_OUT})
    db, agent = await _agent(tmp_path, llm, tools)
    try:
        turn = await agent.run_turn(1, "докупить?")
        assert len(turn.proposals) == 1  # дедуп
    finally:
        await db.close()


async def test_iteration_cap_forces_final(tmp_path: Path) -> None:
    # LLM всегда зовёт инструмент; при лимите — финальный ход с tools=None
    llm = FakeLLM([
        _tools_msg([_tc("get_stock_summary", {})]),
        _tools_msg([_tc("get_stock_summary", {})]),
        _final("Ответ по уже собранным данным"),
    ])
    tools = FakeToolset({"get_stock_summary": '{"articles":[]}'})
    db, agent = await _agent(tmp_path, llm, tools, max_iterations=2)
    try:
        turn = await agent.run_turn(1, "?")
        assert turn.text == "Ответ по уже собранным данным"
        assert llm.calls[-1]["tools"] is None  # финальный ход без инструментов
    finally:
        await db.close()


async def test_llm_error_does_not_persist(tmp_path: Path) -> None:
    llm = FakeLLM([], raise_first=True)
    tools = FakeToolset()
    db, agent = await _agent(tmp_path, llm, tools)
    try:
        turn = await agent.run_turn(1, "вопрос")
        assert "недоступна" in turn.text
        assert turn.proposals == []
        assert await DialogRepository(db).count(1) == 0  # оборванный ход не сохранён
    finally:
        await db.close()


async def test_history_survives_restart(tmp_path: Path) -> None:
    # ход 1 пишет историю; новый агент над тем же db видит её в ходе 2
    tools = FakeToolset()
    db, agent1 = await _agent(tmp_path, FakeLLM([_final("ответ1")]), tools)
    try:
        await agent1.run_turn(1, "вопрос1")

        llm2 = FakeLLM([_final("ответ2")])
        agent2 = CabinetAgent(llm_client=llm2, toolset=tools,  # type: ignore[arg-type]
                              dialog_repo=DialogRepository(db))
        await agent2.run_turn(1, "вопрос2")

        dump = json.dumps(llm2.last_messages, ensure_ascii=False)
        assert "вопрос1" in dump and "ответ1" in dump  # прошлый ход подгружен
    finally:
        await db.close()
