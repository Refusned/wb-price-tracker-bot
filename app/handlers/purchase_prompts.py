from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app.config import AppConfig
from app.storage.business_repository import BusinessRepository
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository
from app.storage.repositories import SubscriberRepository
from app.storage.stock_arrival_repository import StockArrivalRepository

from .common import ensure_allowed, remember_subscriber


class PurchasePromptStates(StatesGroup):
    AwaitingPrice = State()


def get_router(
    config: AppConfig,
    stock_arrival_repo: StockArrivalRepository,
    business_repository: BusinessRepository,
    subscriber_repository: SubscriberRepository,
    decision_snapshot_repo: DecisionSnapshotRepository | None = None,
) -> Router:
    router = Router(name="purchase_prompts")

    @router.message(Command("pending_purchases"))
    async def pending_purchases_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        prompts = await stock_arrival_repo.get_pending(limit=10)
        if not prompts:
            await message.answer("Нет ожидающих prompts по новым закупкам.")
            return

        lines = ["Ожидающие prompts по новым закупкам:", ""]
        for prompt in prompts:
            supplier_article = prompt["supplier_article"] or "—"
            lines.append(
                f"#{prompt['id']}: артикул {prompt['nm_id']} ({supplier_article}), "
                f"+{prompt['qty_delta']} шт, {prompt['detected_at']}"
            )
        lines.extend(
            [
                "",
                "Чтобы ответить, используй inline-кнопки в недавнем сообщении, или вручную /buy",
            ]
        )
        await message.answer("\n".join(lines))

    @router.callback_query(F.data.startswith("purprompt:"))
    async def purchase_prompt_callback(callback: CallbackQuery, state: FSMContext) -> None:
        user = callback.from_user
        if not config.is_user_allowed(user.id if user is not None else None):
            await callback.answer("Доступ запрещён.", show_alert=True)
            return

        data = callback.data or ""
        parts = data.split(":")
        if len(parts) != 3:
            await callback.answer("Некорректная кнопка.", show_alert=True)
            return

        action = parts[1]
        if action not in {"price", "skip"}:
            await callback.answer("Некорректная кнопка.", show_alert=True)
            return

        try:
            prompt_id = int(parts[2])
        except ValueError:
            await callback.answer("Некорректная кнопка.", show_alert=True)
            return

        if action == "skip":
            await stock_arrival_repo.resolve(prompt_id, "ignored")
            await callback.answer("Пропустил.", show_alert=True)
            return

        prompt = await stock_arrival_repo.get_prompt(prompt_id)
        if prompt is None or prompt["status"] != "pending":
            await callback.answer("Уже обработан.", show_alert=True)
            return

        await state.set_state(PurchasePromptStates.AwaitingPrice)
        await state.update_data(
            prompt_id=prompt_id,
            nm_id=prompt["nm_id"],
            supplier_article=prompt["supplier_article"],
            qty_delta=prompt["qty_delta"],
        )
        await callback.answer()
        if callback.message is not None:
            await callback.message.answer(
                "Напиши цену за единицу в ₽ (например 10500). Или /cancel."
            )

    @router.message(PurchasePromptStates.AwaitingPrice)
    async def purchase_prompt_price_handler(message: Message, state: FSMContext) -> None:
        if not await ensure_allowed(message, config):
            return

        text = (message.text or "").strip()
        state_data = await state.get_data()
        prompt_id = int(state_data.get("prompt_id") or 0)

        if text.startswith("/"):
            command = text.split()[0].split("@", 1)[0].lower()
            if command == "/cancel":
                if prompt_id > 0:
                    await stock_arrival_repo.resolve(prompt_id, "cancelled")
                await state.clear()
                await message.answer("Отменено.")
                return

            await message.answer("Напиши цену за единицу в ₽ (например 10500). Или /cancel.")
            return

        try:
            price = float(text.replace(" ", "").replace(",", "."))
        except ValueError:
            await message.answer("Не понял цену. Напиши число, например 10500.")
            return

        if price <= 0:
            await message.answer("Не понял цену. Напиши число, например 10500.")
            return

        prompt = await stock_arrival_repo.get_prompt(prompt_id)
        if prompt is None or prompt["status"] != "pending":
            await state.clear()
            await message.answer("Уже обработан.")
            return

        qty = int(prompt["qty_delta"])
        nm_id = int(prompt["nm_id"])
        supplier_article = prompt["supplier_article"]
        purchase_id = await business_repository.add_purchase(
            nm_id=nm_id,
            supplier_article=supplier_article,
            quantity=qty,
            buy_price_per_unit=price,
            spp_at_purchase=None,
            notes=f"auto from stock arrival prompt #{prompt_id}",
        )

        # Day 16: link to recent decision_snapshot if available. nm_id is
        # always present in auto-prompts (came from stock arrival detector).
        linked_snapshot_id: int | None = None
        if decision_snapshot_repo is not None:
            try:
                snap = await decision_snapshot_repo.find_most_recent_unlinked(
                    nm_id=nm_id, within_seconds=86400,
                )
                if snap is not None:
                    await decision_snapshot_repo.link_to_purchase(
                        snapshot_id=int(snap["id"]),
                        purchase_id=int(purchase_id),
                        action="bought",
                    )
                    linked_snapshot_id = int(snap["id"])
            except Exception:
                pass  # best-effort

        await stock_arrival_repo.resolve(
            prompt_id,
            "replied",
            purchase_id=purchase_id,
        )
        await state.clear()

        total = qty * price
        link_note = f"\n🔗 Связан с decision #{linked_snapshot_id}" if linked_snapshot_id else ""
        await message.answer(
            f"✅ Записал партию: артикул {nm_id} × {qty} шт × "
            f"{_fmt_money(price)}₽ = {_fmt_money(total)}₽. "
            "Сохранил в /purchases. "
            "Лот будет привязан после следующего lot ledger build."
            f"{link_note}"
        )

    return router


def _fmt_money(value: float) -> str:
    text = f"{float(value):.2f}"
    if text.endswith(".00"):
        return text[:-3]
    if text.endswith("0"):
        return text[:-1]
    return text
