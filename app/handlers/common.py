from __future__ import annotations

import html
from datetime import datetime, timezone

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message

from app.config import AppConfig
from app.scheduler import WbUpdateScheduler
from app.storage.repositories import (
    ItemRepository,
    MetaRepository,
    SettingsRepository,
    SubscriberRepository,
)
from app.utils.formatting import format_iso_datetime, format_price_rub


def is_cache_stale(last_success_iso: str | None, max_age_seconds: int) -> bool:
    if not last_success_iso:
        return True
    try:
        dt = datetime.fromisoformat(last_success_iso)
    except ValueError:
        return True

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
    return age_seconds > max_age_seconds


async def ensure_allowed(message: Message, config: AppConfig) -> bool:
    user_id = message.from_user.id if message.from_user else None
    if config.is_user_allowed(user_id):
        return True

    await message.answer("Доступ запрещён.")
    return False


async def answer_safe(message: Message, text: str, **kwargs) -> None:
    """Ответ LLM-текстом: пробуем Markdown, при битой разметке — plain.

    Модель просят писать без разметки, но нечётные */_ в тексте (или сущность,
    разрезанная обрезкой лимита) валят parse — тогда шлём как есть.
    """
    try:
        await message.answer(text, parse_mode="Markdown", **kwargs)
    except TelegramBadRequest:
        await message.answer(text, **kwargs)


async def remember_subscriber(message: Message, subscriber_repository: SubscriberRepository) -> None:
    chat = message.chat
    if chat is None:
        return
    if chat.type != "private":
        return

    user_id = message.from_user.id if message.from_user else None
    await subscriber_repository.upsert_active(chat.id, user_id)


def get_router(
    config: AppConfig,
    item_repository: ItemRepository,
    meta_repository: MetaRepository,
    settings_repository: SettingsRepository,
    subscriber_repository: SubscriberRepository,
    updater: WbUpdateScheduler,
) -> Router:
    router = Router(name="common")

    # NB: /start обрабатывает main_menu (зарегистрирован раньше в bot.py).
    # Здесь его нет намеренно — был дублирующий недостижимый хендлер.

    @router.message(Command("help"))
    async def help_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        help_lines = [
            "<b>📖 Команды бота</b>",
            "",
            "<b>💼 Бизнес — ежедневное</b>",
            "/briefing — утренний дайджест",
            "/today /yesterday /week /month — метрики за период",
            "/profit — чистая прибыль (пример: <code>/profit week</code>)",
            "/stock — остатки на складах (FBO + FBS)",
            "/stock_fbs — принудительный запрос FBS",
            "/finance — удержания из финотчёта",
            "/cashflow — кэшфлоу и ожидаемая выплата",
            "",
            "<b>📦 Закупки и решения</b>",
            "/buy — записать закупку (пример: <code>/buy 20 9500 019</code>)",
            "/purchases — закупки за 30 дней",
            "/pending_purchases — партии, ждущие цену закупки",
            "/reorder — когда и сколько дозаказать",
            "/decisions — снимки решений по алертам",
            "/decision_stats — статистика решений за 30 дней",
            "/missed_deals — разбор упущенных сделок",
            "",
            "<b>📈 Аналитика</b>",
            "/abc — ABC-анализ ассортимента",
            "/returns — возвраты за 30 дней",
            "/insights — аномалии продаж",
            "/advice — LLM-разбор кабинета с советами",
            "",
            "<b>🤖 Ассистент</b>",
            "Кнопка «🤖 Ассистент» или /chat — диалог с ИИ по кабинету:",
            "сам читает данные, проверяет настройки и предлагает правки —",
            "каждое изменение подтверждаешь кнопкой",
            "/stop — выйти из диалога",
            "",
            "<b>🔎 Цены и сделки</b>",
            "/top10 — топ-10 дешёвых по запросу",
            "/deals — сделки с маржой выше порога",
            "/calc — маржа при цене закупки (пример: <code>/calc 9500</code>)",
            "/find_deal — свежие дешёвые карточки немедленно",
            "/track /untrack /tracked /untrack_all — отслеживание артикулов",
            "",
            "<b>💸 СПП</b>",
            "/spp — текущий СПП и расчёт",
            "/setspp — изменить СПП (пример: <code>/setspp 24</code>)",
            "/setspp_log — записать снимок личного СПП",
            "/spp_history — история личного СПП",
            "/spp_trend — тренд СПП за 7 дней",
            "/refresh_spp — собрать СПП из свежих продаж",
            "",
            "<b>🎯 Арбитраж</b>",
            "/arb — меню сканера связок (все /arb_* внутри)",
            "",
            "<b>⚙️ Служебное</b>",
            "/status — состояние бота и кэша",
            "/menu — главное меню",
            "/rescan /rescan_seller — принудительное обновление",
            "/resync_history — пересинхронизация истории (90 дней)",
            "/sync_finance — подтянуть финотчёт WB",
            "/costs /profitcosts — параметры маржи и прибыли",
            "/settax /setlogistics /setacquiring — налог, логистика, эквайринг",
            "/setsellprice /setminprice /setalertcooldown — цена, минцена, кулдаун",
            "/alerts_on /alerts_off — уведомления о ценах",
        ]
        await message.answer("\n".join(help_lines), parse_mode="HTML")

    @router.message(Command("status"))
    async def status_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        items_count = await item_repository.count_items()
        subscribers_count = await subscriber_repository.count_active()
        min_price = await settings_repository.get_min_price_rub(config.min_price_rub)

        last_success = await meta_repository.get_value("last_success_update_at")
        last_status = await meta_repository.get_value("last_update_status")
        last_error = await meta_repository.get_value("last_update_error")
        last_alerts_sent = await meta_repository.get_value("last_alerts_sent")
        last_tracked_count = await meta_repository.get_value("last_tracked_count")
        last_search_status = await meta_repository.get_value("last_search_status")
        alert_cooldown = int(await settings_repository.get_float("alert_cooldown_minutes", 30.0))

        stale = is_cache_stale(last_success, config.max_cache_age_seconds)

        lines = [
            "<b>📊 Статус бота</b>",
            "",
            f"Запрос: {html.escape(config.wb_query)}",
            f"Товаров в кэше: <b>{items_count}</b>",
            f"Отслеживаемых артикулов: {last_tracked_count or '—'}",
            f"Фильтр мин. цены: {format_price_rub(min_price)}",
            "",
            f"Последний скан: <b>{format_iso_datetime(last_success)}</b>",
            f"Статус: {last_status or 'нет данных'}",
            f"Search запускался: {last_search_status or 'нет данных'}",
            f"Интервал: {config.wb_poll_interval_seconds} сек",
            f"Кэш протух: {'⚠️ да' if stale else '✅ нет'}",
            "",
            f"Уведомления: {'✅ вкл' if config.alerts_enabled else '❌ выкл'}",
            f"Порог падения: -{config.alert_drop_percent:.1f}%",
            f"Cooldown между алертами: {alert_cooldown} мин",
            f"Подписчиков: {subscribers_count}",
            f"Отправлено в посл. скан: {last_alerts_sent or 0}",
        ]

        if last_status == "error" and last_error:
            lines.append("")
            lines.append(f"⚠️ Ошибка: {html.escape(last_error[:300])}")

        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("alerts_on"))
    async def alerts_on_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return

        await remember_subscriber(message, subscriber_repository)
        if message.chat:
            await subscriber_repository.set_active(message.chat.id, True)
        await message.answer("Push-уведомления о резком снижении цены включены.")

    @router.message(Command("alerts_off"))
    async def alerts_off_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return

        if message.chat:
            await subscriber_repository.set_active(message.chat.id, False)
        await message.answer("Push-уведомления выключены для этого чата.")

    return router
