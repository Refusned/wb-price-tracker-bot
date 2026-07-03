"""
Хендлеры режима «🤖 Ассистент» — интерактивный диалог с LLM-агентом по кабинету
(Фаза 3).

Вход — кнопка меню (AGENT_BUTTON) или /chat. Внутри FSM-состояния Active весь
свободный текст уходит в CabinetAgent.run_turn; команды и кнопки меню продолжают
работать (роутер регистрируется ПОСЛЕДНИМ, текст-хендлер ловит только не-команды).

Money-safety: агент только читает и СОВЕТУЕТ. Предложенные им мутации (закупка/
настройка/ответ покупателю) исполняются ТОЛЬКО по нажатию подписанной (HMAC)
inline-кнопки — здесь, через существующий код, с re-валидацией. Публичный ответ
покупателю по подтверждению публикуется сразу (без shadow-гейта) — защита
контент-гейтом + идемпотентностью. Пендинги — в FSM data по НЕ сбрасываемому в
рамках сессии монотонному id (старая кнопка не коллизирует с новым предложением).
"""
from __future__ import annotations

import logging
import math
from dataclasses import asdict
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.chat_action import ChatActionSender

from app.config import AppConfig
from app.handlers.common import ensure_allowed, remember_subscriber
from app.handlers.main_menu import AGENT_BUTTON, main_menu_keyboard
from app.security import sign_payload, verify_payload
from app.services.agent_tools import SETTINGS_INT_KEYS, SETTINGS_PARAMS
from app.services.cabinet_agent import CabinetAgent
from app.services.feedback_posting import finalize_answer, post_reply_idempotent
from app.storage.business_repository import BusinessRepository
from app.storage.feedback_reply_repository import FeedbackReplyRepository
from app.storage.repositories import SettingsRepository, SubscriberRepository
from app.wb.feedbacks_client import WBFeedbacksClient

logger = logging.getLogger("agent_chat")

STOP_BTN = "⏹ Завершить диалог"
NEW_BTN = "🆕 Новый диалог"
_REPLY_LIMIT = 3900
# Один источник правды с propose_setting (agent_tools.SETTINGS_PARAMS).
_VALID_SETTINGS_KEYS = {spec[0] for spec in SETTINGS_PARAMS.values()}


class AgentChatStates(StatesGroup):
    Active = State()


def _dialog_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=STOP_BTN), KeyboardButton(text=NEW_BTN)]],
        resize_keyboard=True, is_persistent=True,
    )


def get_router(
    *,
    config: AppConfig,
    cabinet_agent: CabinetAgent,
    subscriber_repository: SubscriberRepository,
    business_repository: BusinessRepository,
    settings_repository: SettingsRepository,
    feedbacks_client: WBFeedbacksClient | None = None,
    reply_repo: FeedbackReplyRepository | None = None,
) -> Router:
    router = Router(name="agent_chat")
    secret = config.callback_signing_secret

    async def _enter(message: Message, state: FSMContext) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)
        # pending очищаем, но next_pid НЕ сбрасываем — иначе старая (не нажатая)
        # кнопка из прошлого диалога могла бы исполнить НОВОЕ предложение на том
        # же pid. Монотонный счётчик в рамках FSM-сессии исключает коллизию.
        prev = await state.get_data()
        next_pid = int(prev.get("next_pid") or 0)
        await state.set_state(AgentChatStates.Active)
        await state.update_data(pending={}, next_pid=next_pid, busy=False)
        await message.answer(
            "🤖 Режим диалога по кабинету включён.\n\n"
            "Спрашивай свободным текстом: «почему упали продажи по 019», «что выгоднее "
            "докупить на этой неделе». Я сам подтяну данные кабинета (только чтение), "
            "доведу разбор до конца и при необходимости предложу действие с кнопкой "
            "подтверждения.\n\nВыйти: «⏹ Завершить диалог» или /stop.",
            reply_markup=_dialog_keyboard(),
        )

    async def _exit(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("✅ Диалог завершён.", reply_markup=main_menu_keyboard())

    # ---- вход (кнопка меню / алиас /chat) — без фильтра состояния ----
    @router.message(F.text == AGENT_BUTTON)
    async def enter_button(message: Message, state: FSMContext) -> None:
        await _enter(message, state)

    @router.message(Command("chat"))
    async def enter_command(message: Message, state: FSMContext) -> None:
        await _enter(message, state)

    # ---- выход — без фильтра состояния (переживает рестарт: кнопка не «мёртвая») ----
    @router.message(Command("stop"))
    async def stop_command(message: Message, state: FSMContext) -> None:
        await _exit(message, state)

    @router.message(F.text == STOP_BTN)
    async def stop_button(message: Message, state: FSMContext) -> None:
        await _exit(message, state)

    @router.message(AgentChatStates.Active, F.text == NEW_BTN)
    async def new_dialog(message: Message, state: FSMContext) -> None:
        await cabinet_agent.reset(message.chat.id)
        prev = await state.get_data()
        next_pid = int(prev.get("next_pid") or 0)  # монотонен, не сбрасываем
        await state.update_data(pending={}, next_pid=next_pid)
        await message.answer("🆕 Начат новый диалог — прошлый контекст очищен.",
                             reply_markup=_dialog_keyboard())

    # ---- ход диалога: свободный текст в Active (НЕ команды, НЕ кнопки режима) ----
    @router.message(
        AgentChatStates.Active,
        F.text,
        ~F.text.startswith("/"),
        ~F.text.in_({STOP_BTN, NEW_BTN, AGENT_BUTTON}),
    )
    async def dialog_turn(message: Message, state: FSMContext) -> None:
        if not await ensure_allowed(message, config):
            return
        data = await state.get_data()
        if data.get("busy"):
            await message.answer("⏳ Ещё думаю над прошлым вопросом, секунду…")
            return
        pending: dict[str, Any] = dict(data.get("pending") or {})
        next_pid = int(data.get("next_pid") or 0)

        await state.update_data(busy=True)
        try:
            async with ChatActionSender.typing(bot=message.bot, chat_id=message.chat.id):
                turn = await cabinet_agent.run_turn(message.chat.id, message.text or "")
            await message.answer((turn.text or "…")[:_REPLY_LIMIT])
            for proposal in turn.proposals:
                pid = str(next_pid)
                next_pid += 1
                pending[pid] = asdict(proposal)
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Подтвердить",
                                         callback_data=sign_payload(f"agent:do:{pid}", secret)),
                    InlineKeyboardButton(text="✖️ Отмена",
                                         callback_data=sign_payload(f"agent:cancel:{pid}", secret)),
                ]])
                await message.answer(f"Подтвердить действие?\n{proposal.summary}", reply_markup=kb)
            await state.update_data(pending=pending, next_pid=next_pid)
        except Exception:  # noqa: BLE001
            logger.exception("agent dialog_turn failed")
            await message.answer("❌ Не смог обработать запрос. Подробности в логах.")
        finally:
            await state.update_data(busy=False)

    # ---- не-текст в Active (фото/стикеры): не глушим команды (у них есть text) ----
    @router.message(AgentChatStates.Active, ~F.text)
    async def active_non_text(message: Message) -> None:
        await message.answer("Я понимаю только текст. Напиши вопрос или «⏹ Завершить диалог».")

    # ---- подтверждение/отмена предложенного действия ----
    @router.callback_query(F.data.startswith("agent:"))
    async def agent_callback(callback: CallbackQuery, state: FSMContext) -> None:
        payload = verify_payload(callback.data, secret)
        if payload is None:
            await callback.answer("Кнопка устарела или повреждена.", show_alert=True)
            return
        parts = payload.split(":")
        if len(parts) != 3 or parts[0] != "agent":
            await callback.answer()
            return
        action, pid = parts[1], parts[2]

        data = await state.get_data()
        pending: dict[str, Any] = dict(data.get("pending") or {})
        proposal = pending.pop(pid, None)
        await state.update_data(pending=pending)

        if proposal is None:
            await callback.answer("Предложение устарело.", show_alert=True)
            return
        if action == "cancel":
            await callback.answer("Отменено")
            await _safe_edit(callback, "✖️ Отменено.")
            return
        if action != "do":
            await callback.answer()
            return

        await callback.answer("Выполняю…")
        result = await execute_action(
            proposal,
            business_repository=business_repository,
            settings_repository=settings_repository,
            feedbacks_client=feedbacks_client,
            reply_repo=reply_repo,
            config=config,
        )
        await _safe_edit(callback, result)

    return router


async def execute_action(
    proposal: dict[str, Any],
    *,
    business_repository: BusinessRepository,
    settings_repository: SettingsRepository,
    feedbacks_client: WBFeedbacksClient | None,
    reply_repo: FeedbackReplyRepository | None,
    config: AppConfig,
) -> str:
    """Исполнить подтверждённое действие через существующий код. Модульная (не
    closure) — тестируется напрямую. MS-3: re-валидация (не доверяем сохранённому
    пендингу слепо). Реальный публичный ответ покупателю публикуется СРАЗУ по
    подтверждению — защита: HMAC-подпись кнопки + контент-гейт + идемпотентность
    (FeedbackReplyRepository) + доступ только владельца (deny-by-default)."""
    kind = proposal.get("kind")
    params = proposal.get("params") or {}
    try:
        if kind == "purchase":
            qty = int(params.get("quantity"))
            price = float(params.get("buy_price_per_unit"))
            if qty <= 0 or price <= 0:
                return "❌ Некорректные параметры закупки."
            new_id = await business_repository.add_purchase(
                nm_id=params.get("nm_id"),
                supplier_article=params.get("supplier_article"),
                quantity=qty, buy_price_per_unit=price,
                spp_at_purchase=None, notes=params.get("notes"),
            )
            return f"✅ Закупка записана (#{new_id}): {qty} шт × {price:g} ₽."

        if kind == "profit_setting":
            key = params.get("settings_key")
            if key not in _VALID_SETTINGS_KEYS:
                return "❌ Неизвестный параметр настроек."
            value = float(params.get("value"))
            if not math.isfinite(value):
                return "❌ Значение должно быть конечным числом."
            # set_value + get_float-читатели (в SettingsRepository нет set_float).
            # Целочисленные ключи храним как int: get_min_price_rub парсит int(raw)
            # и молча откатился бы на дефолт от строки «9500.0».
            stored = str(int(round(value))) if key in SETTINGS_INT_KEYS else str(value)
            await settings_repository.set_value(key, stored)
            return f"✅ Параметр обновлён: {key} = {stored}."

        if kind == "feedback_reply":
            if feedbacks_client is None or reply_repo is None:
                return "❌ Ответы покупателям недоступны (нет ключа «Вопросы и отзывы»)."
            target_id = str(params.get("target_id") or "")
            target_kind = params.get("target_kind")
            if target_kind not in ("feedback", "question") or not target_id:
                return "❌ Некорректная цель ответа."
            answer = finalize_answer(str(params.get("text") or ""), config.feedback_signature or "")
            if answer is None:
                return "❌ Ответ не прошёл контент-гейт (ссылки/телефон/почта или слишком короткий)."
            if await reply_repo.is_handled(target_kind, target_id):
                return "⚠️ На это уже отвечено (или исчерпан лимит попыток)."

            if target_kind == "feedback":
                async def publish(text: str) -> None:
                    await feedbacks_client.answer_feedback(target_id, text)
            else:
                async def publish(text: str) -> None:
                    await feedbacks_client.answer_question(target_id, text)

            ok, status = await post_reply_idempotent(
                reply_repo, kind=target_kind, feedback_id=target_id,
                original_text="(из диалога-агента)", answer=answer, publish=publish,
            )
            return "✅ Ответ опубликован покупателю." if ok else f"❌ Не опубликовано ({status})."

        return "❌ Неизвестное действие."
    except Exception:  # noqa: BLE001
        logger.exception("agent action '%s' failed", kind)
        return "❌ Ошибка выполнения. Подробности в логах."


async def _safe_edit(callback: CallbackQuery, text: str) -> None:
    """Заменить текст сообщения (убрать кнопки). Не падаем на edit-ошибках."""
    try:
        if callback.message is not None:
            await callback.message.edit_text(text)
    except Exception:  # noqa: BLE001
        try:
            if callback.message is not None:
                await callback.message.answer(text)
        except Exception:  # noqa: BLE001
            pass
