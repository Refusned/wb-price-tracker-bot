"""Main menu reply-keyboard navigation.

Reply-keyboard layout shown after /start and on '↩️ Главное меню' tap from
submenus:
    [🎯 Арбитраж]   [💰 Финансы]
    [📊 Аналитика]  [⚙️ Настройки]

Each button routes to existing handlers/commands. Arbitrage opens its own
submenu. Финансы → /finance. Аналитика → /briefing. Настройки → /help.
"""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.config import AppConfig
from app.handlers.common import ensure_allowed, remember_subscriber
from app.storage.repositories import SubscriberRepository


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎯 Арбитраж"), KeyboardButton(text="💰 Финансы")],
            [KeyboardButton(text="📊 Аналитика"), KeyboardButton(text="⚙️ Настройки")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_router(config: AppConfig, subscriber_repo: SubscriberRepository) -> Router:
    router = Router(name="main_menu")

    async def _show_main(message: Message) -> None:
        await message.answer(
            "🏠 *Главное меню*\n\n"
            "Выбери раздел:\n"
            "• 🎯 Арбитраж — автономный сканер связок\n"
            "• 💰 Финансы — выручка, прибыль, ABC-анализ\n"
            "• 📊 Аналитика — daily briefing\n"
            "• ⚙️ Настройки — команды и параметры",
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown",
        )

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repo)
        await _show_main(message)

    @router.message(Command("menu"))
    async def cmd_menu(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await _show_main(message)

    # Reply-keyboard button handlers
    @router.message(lambda m: m.text in ("↩️ Главное меню", "🔙 Главное меню",
                                          "Главное меню", "🏠 Главное меню"))
    async def back_to_main(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await _show_main(message)

    @router.message(lambda m: m.text == "💰 Финансы")
    async def go_finance(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await message.answer(
            "💰 *Финансы*\n\n"
            "Команды:\n"
            "• /finance — отчёт за период\n"
            "• /abc — ABC-анализ артикулов\n"
            "• /returns — последние возвраты\n"
            "• /margin — расчёт маржи\n"
            "• /buy — записать закупку\n"
            "• /calc — калькулятор маржи\n"
            "• /settax, /setlogistics, /setacquiring — настройки",
            parse_mode="Markdown",
        )

    @router.message(lambda m: m.text == "📊 Аналитика")
    async def go_analytics(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await message.answer(
            "📊 *Аналитика*\n\n"
            "Команды:\n"
            "• /briefing — утренний дайджест\n"
            "• /top10 — топ-10 цен по основному запросу\n"
            "• /insights — anomaly / shadow-ban\n"
            "• /spp_trend — динамика моей СПП\n"
            "• /decisions — последние решения бота",
            parse_mode="Markdown",
        )

    @router.message(lambda m: m.text == "⚙️ Настройки")
    async def go_settings(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await message.answer(
            "⚙️ *Настройки*\n\n"
            "Команды:\n"
            "• /help — все команды\n"
            "• /setmin <₽> — минимальная цена для /top10\n"
            "• /status — статус сканера\n"
            "• /rescan — принудительный скан\n"
            "• /track add/remove <nm> — tracking SKU",
            parse_mode="Markdown",
        )

    return router
