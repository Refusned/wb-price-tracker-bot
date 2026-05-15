from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import AppConfig
from app.storage.missed_deal_repository import MISSED_DEAL_REASONS, MissedDealRepository
from app.storage.repositories import SubscriberRepository

from .common import ensure_allowed, remember_subscriber


class MissedDealStates(StatesGroup):
    Tagging = State()


def get_router(
    config: AppConfig,
    missed_deal_repo: MissedDealRepository,
    subscriber_repository: SubscriberRepository,
) -> Router:
    router = Router(name="missed_deals")

    @router.message(Command("missed_deals"))
    async def missed_deals_handler(message: Message, state: FSMContext) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        candidates = await missed_deal_repo.find_untagged_candidates(limit=15)
        if not candidates:
            await message.answer(
                "🎯 Нет новых missed-deal кандидатов. "
                "Бот пока не видел заметных падений цены без твоей покупки."
            )
            return

        await state.set_state(MissedDealStates.Tagging)
        await state.update_data(candidates=candidates, index=0, tagged=0)
        await _send_candidate(message, candidates[0])

    @router.callback_query(MissedDealStates.Tagging, F.data.startswith("md:"))
    async def missed_deal_callback(callback: CallbackQuery, state: FSMContext) -> None:
        data = callback.data or ""
        await callback.answer()

        if data == "md:stop":
            await _finish_session(callback, state, missed_deal_repo)
            return

        state_data = await state.get_data()
        candidates = list(state_data.get("candidates") or [])
        index = int(state_data.get("index") or 0)
        tagged = int(state_data.get("tagged") or 0)

        if index >= len(candidates):
            await _finish_session(callback, state, missed_deal_repo)
            return

        candidate = candidates[index]

        if data != "md:skip":
            parts = data.split(":", 3)
            if len(parts) != 4:
                return
            reason = parts[1]
            if reason not in MISSED_DEAL_REASONS:
                return

            inserted = await missed_deal_repo.tag(
                nm_id=int(candidate["nm_id"]),
                candidate_date=str(candidate["candidate_date"]),
                reason=reason,
                observed_price=float(candidate["observed_price"]),
                observed_margin_estimate=float(candidate["observed_margin_estimate"]),
            )
            if inserted:
                tagged += 1

        index += 1
        if index >= len(candidates):
            await state.update_data(index=index, tagged=tagged)
            await _finish_session(callback, state, missed_deal_repo)
            return

        await state.update_data(index=index, tagged=tagged)
        if callback.message is not None:
            await _send_candidate(callback.message, candidates[index])

    return router


async def _send_candidate(message: Message, candidate: dict) -> None:
    text = (
        f"Артикул {candidate['nm_id']} ({candidate['name']})\n"
        f"Цена упала: {_money(candidate['prev_price'])}₽ → "
        f"{_money(candidate['observed_price'])}₽ "
        f"(-{_fmt(candidate['drop_pct'])}%)\n"
        f"Дата: {candidate['candidate_date']}\n"
        f"Прогнозная маржа: ~{_fmt(candidate['observed_margin_estimate'])}%\n"
        "Почему не купил?"
    )
    await message.answer(text, reply_markup=_keyboard(candidate))


def _keyboard(candidate: dict) -> InlineKeyboardMarkup:
    nm_id = int(candidate["nm_id"])
    candidate_date = str(candidate["candidate_date"])
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="cash", callback_data=f"md:cash:{nm_id}:{candidate_date}"),
                InlineKeyboardButton(text="too_slow", callback_data=f"md:too_slow:{nm_id}:{candidate_date}"),
            ],
            [
                InlineKeyboardButton(text="bad_margin", callback_data=f"md:bad_margin:{nm_id}:{candidate_date}"),
                InlineKeyboardButton(
                    text="not_interested",
                    callback_data=f"md:not_interested:{nm_id}:{candidate_date}",
                ),
            ],
            [
                InlineKeyboardButton(text="skip", callback_data="md:skip"),
                InlineKeyboardButton(text="✋ stop", callback_data="md:stop"),
            ],
        ]
    )


async def _finish_session(
    callback: CallbackQuery,
    state: FSMContext,
    missed_deal_repo: MissedDealRepository,
) -> None:
    state_data = await state.get_data()
    tagged = int(state_data.get("tagged") or 0)
    await state.clear()

    distribution = await missed_deal_repo.distribution()
    text = (
        f"Тег'нул {tagged} сделок. Распределение: "
        f"cash={distribution.get('cash', 0)}, "
        f"too_slow={distribution.get('too_slow', 0)}, "
        f"bad_margin={distribution.get('bad_margin', 0)}, "
        f"not_interested={distribution.get('not_interested', 0)}. "
        "Day 6 GATE будет использовать это для выбора направления."
    )
    if callback.message is not None:
        await callback.message.answer(text)


def _fmt(value: float) -> str:
    text = f"{float(value):.1f}"
    return text[:-2] if text.endswith(".0") else text


def _money(value: float) -> str:
    return f"{float(value):.0f}"
