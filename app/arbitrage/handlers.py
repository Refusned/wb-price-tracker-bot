"""/arb_* handlers for the arbitrage submodule.

Tier 1 commands (Day 18):
    /arb              — submenu
    /arb_add <query>  — add new scan query
    /arb_list         — enabled queries
    /arb_remove <id>  — soft-disable query
    /arb_deals        — top recent candidates (24h)
    /arb_my_spp       — observed buyer-side СПП per category
    /arb_top_cat      — top categories by AVG observed СПП
    /arb_observe nm cena_pol_chek_aut public_cena  — record manual observation
    /arb_scan_now     — trigger immediate scan_once

All commands restricted by ALLOWED_USER_IDS (ensure_allowed).
"""
from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from app.arbitrage.repository import ArbitrageRepository
from app.arbitrage.scanner import ArbitrageScanner
from app.config import AppConfig
from app.handlers.common import ensure_allowed, remember_subscriber
from app.storage.repositories import SubscriberRepository

logger = logging.getLogger(__name__)


def _arb_submenu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 Свежие связки"), KeyboardButton(text="📋 Мои запросы")],
            [KeyboardButton(text="📊 Моя СПП"), KeyboardButton(text="🏆 Топ категории")],
            [KeyboardButton(text="↩️ Главное меню")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def get_router(
    config: AppConfig,
    arb_repo: ArbitrageRepository,
    scanner: ArbitrageScanner,
    subscriber_repo: SubscriberRepository,
) -> Router:
    router = Router(name="arbitrage")

    # ── /arb (submenu) ──────────────────────────────────────────
    @router.message(Command("arb"))
    async def arb_root(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repo)
        await message.answer(
            "🎯 *Арбитражный сканер*\n\n"
            "Команды:\n"
            "• `/arb_add <фраза>` — добавить запрос\n"
            "• `/arb_list` — мои запросы\n"
            "• `/arb_remove <id>` — отключить запрос\n"
            "• `/arb_deals` — свежие связки (24ч)\n"
            "• `/arb_my_spp` — моя СПП по категориям\n"
            "• `/arb_top_cat` — топ категорий по СПП\n"
            "• `/arb_observe <nm> <pol_chek> <bez_skidki>` — записать наблюдение\n"
            "• `/arb_scan_now` — запустить скан сейчас",
            reply_markup=_arb_submenu_keyboard(),
            parse_mode="Markdown",
        )

    # ── /arb_add ────────────────────────────────────────────────
    @router.message(Command("arb_add"))
    async def arb_add(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        text = (message.text or "").split(maxsplit=1)
        if len(text) < 2 or not text[1].strip():
            await message.answer("Использование: `/arb_add Робот пылесос`", parse_mode="Markdown")
            return
        query = text[1].strip()
        try:
            qid = await arb_repo.add_query(query)
            await message.answer(f"✅ Запрос #{qid} добавлен: «{query}»")
        except Exception as exc:
            logger.exception("arb_add failed")
            await message.answer(f"❌ Ошибка: {exc}")

    # ── /arb_list ───────────────────────────────────────────────
    @router.message(Command("arb_list"))
    async def arb_list(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        rows = await arb_repo.list_queries(only_enabled=True)
        if not rows:
            await message.answer("Нет активных запросов. Добавь через `/arb_add <фраза>`.",
                                  parse_mode="Markdown")
            return
        lines = ["📋 *Мои запросы:*", ""]
        hints: list[str] = []
        for r in rows:
            last = (r["last_scanned_at"] or "—")[:16]
            subj_info = ""
            if r.get("subject_id"):
                subj_info = f" → subj #{r['subject_id']}"
                if r.get("subject_name"):
                    subj_info = f" → {r['subject_name']}"
            lines.append(
                f"#{r['id']} «{r['query']}»{subj_info}\n"
                f"   В каталоге: {r['last_found_count']}, последний скан: {last}"
            )
            # Hint: if cohort found but no observations in that category yet
            if r.get("subject_id") and r["last_found_count"] > 0:
                cat = await arb_repo.get_category_avg_spp(
                    r["subject_id"], days=30, min_samples=1,
                )
                if cat is None or cat.get("samples", 0) < 3:
                    hints.append(
                        f"⚠️ #{r['id']} «{r['query']}» — нет СПП-наблюдений "
                        f"для категории #{r['subject_id']}. "
                        f"Чтобы получать алерты, добавь 3+ наблюдения: "
                        f"`/arb_observe <nm> <моя_цена> <публич>`"
                    )
        if hints:
            lines.append("")
            lines.append("━━━━━━━━━━━━━━━━━━━")
            lines.extend(hints)
        await message.answer("\n".join(lines), parse_mode="Markdown")

    # ── /arb_remove ─────────────────────────────────────────────
    @router.message(Command("arb_remove"))
    async def arb_remove(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: `/arb_remove <id или фраза>`", parse_mode="Markdown")
            return
        ident = parts[1].strip()
        await arb_repo.remove_query(ident)
        await message.answer(f"✅ Запрос «{ident}» отключён")

    # ── /arb_deals ──────────────────────────────────────────────
    @router.message(Command("arb_deals"))
    async def arb_deals(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        cands = await arb_repo.recent_candidates(hours=24, limit=10)
        if not cands:
            await message.answer("За 24 часа связок не найдено. Добавь запросы через `/arb_add`.",
                                  parse_mode="Markdown")
            return
        lines = ["🔥 *Свежие связки (24ч):*", ""]
        for c in cands:
            mark = "🚨" if c.get("alerted_at") else "·"
            lines.append(
                f"{mark} nm {c['nm_id']} | margin {c['margin_percent']:.1f}% "
                f"({c['margin_rub']}₽) | ROI/д {c['profit_per_ruble_day_pct']:.2f}%"
            )
            name = c.get("name") or ""
            if name:
                lines.append(f"   {name[:60]}")
        await message.answer("\n".join(lines), parse_mode="Markdown")

    # ── /arb_my_spp ─────────────────────────────────────────────
    @router.message(Command("arb_my_spp"))
    async def arb_my_spp(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        top = await arb_repo.top_categories_by_spp(days=30, min_samples=1, limit=20)
        if not top:
            await message.answer(
                "Наблюдений нет. Запиши через `/arb_observe <nm> <моя_цена> <публич_цена>`.",
                parse_mode="Markdown",
            )
            return
        lines = ["📊 *Моя buyer-side СПП по категориям (30д):*", ""]
        for c in top:
            lines.append(
                f"• {c['subject_name']}: AVG {c['avg_spp']:.1f}% "
                f"(samples={c['samples']})"
            )
        await message.answer("\n".join(lines), parse_mode="Markdown")

    # ── /arb_top_cat ────────────────────────────────────────────
    @router.message(Command("arb_top_cat"))
    async def arb_top_cat(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        top = await arb_repo.top_categories_by_spp(days=30, min_samples=3, limit=5)
        if not top:
            await message.answer(
                "Нет категорий с ≥3 наблюдениями. Добавь больше через `/arb_observe`.",
                parse_mode="Markdown",
            )
            return
        lines = ["🏆 *Топ-5 категорий с моей высокой СПП:*", ""]
        for i, c in enumerate(top, 1):
            lines.append(
                f"{i}. {c['subject_name']} — {c['avg_spp']:.1f}% (n={c['samples']})"
            )
        lines.append("\nДобавляй запросы в эти категории через `/arb_add`.")
        await message.answer("\n".join(lines), parse_mode="Markdown")

    # ── /arb_observe ────────────────────────────────────────────
    @router.message(Command("arb_observe"))
    async def arb_observe(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        parts = (message.text or "").split()
        if len(parts) < 4:
            await message.answer(
                "Использование: `/arb_observe <nm_id> <моя_цена_на_checkout> <публичная_цена>`\n\n"
                "Пример: `/arb_observe 876392996 10658 15000`\n\n"
                "Где `публичная_цена` — цена без личной СПП (та что видит обычный покупатель).",
                parse_mode="Markdown",
            )
            return
        try:
            nm_id = int(parts[1])
            my_price = int(parts[2])
            public_price = int(parts[3])
        except ValueError:
            await message.answer("Неверный формат чисел.")
            return

        try:
            obs_id = await arb_repo.record_spp_observation(
                nm_id=nm_id,
                subject_id=None,
                subject_name=None,
                public_price_rub=public_price,
                my_buyer_price_rub=my_price,
                source="checkout_manual",
                confidence="high",
                sample_count=1,
                note="manual /arb_observe",
            )
            spp_pct = (1 - my_price / public_price) * 100.0
            await message.answer(
                f"✅ Наблюдение #{obs_id} записано.\n"
                f"nm {nm_id}: моя СПП = *{spp_pct:.1f}%* "
                f"({public_price - my_price}₽ от {public_price}₽)",
                parse_mode="Markdown",
            )
        except Exception as exc:
            logger.exception("arb_observe failed")
            await message.answer(f"❌ Ошибка: {exc}")

    # ── /arb_scan_now ───────────────────────────────────────────
    @router.message(Command("arb_scan_now"))
    async def arb_scan_now(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await message.answer("⏳ Запускаю скан…")
        try:
            result = await scanner.scan_once()
            # Build summary with per-query breakdown
            queries = await arb_repo.list_queries(only_enabled=True)
            lines = [
                f"✅ Скан завершён.",
                f"Запросов: {result['queries']}",
                f"Кандидатов (с СПП): {result['candidates']}",
                f"Отправлено алертов: {result['alerted']}",
                "",
                "*По запросам:*",
            ]
            need_obs: list[str] = []
            for q in queries:
                found = q.get("last_found_count", 0)
                subj = q.get("subject_name") or (f"subj#{q['subject_id']}" if q.get("subject_id") else "?")
                lines.append(f"• «{q['query']}» → {subj}: {found} товаров")
                if q.get("subject_id") and found > 0:
                    cat = await arb_repo.get_category_avg_spp(
                        q["subject_id"], days=30, min_samples=1,
                    )
                    if cat is None or cat.get("samples", 0) < 3:
                        need_obs.append(
                            f"• {subj} (нужно 3+ наблюдения)"
                        )
            if need_obs:
                lines.append("")
                lines.append("⚠️ *Категории без СПП-данных:*")
                lines.extend(need_obs)
                lines.append("")
                lines.append("Добавь `/arb_observe <nm> <моя_цена> <публич_цена>` "
                             "для 3+ товаров в этих категориях — сканер начнёт алертить.")
            await message.answer("\n".join(lines), parse_mode="Markdown")
        except Exception as exc:
            logger.exception("arb_scan_now failed")
            await message.answer(f"❌ Ошибка сканера: {exc}")

    # ── Reply-keyboard buttons (submenu shortcuts) ─────────────
    @router.message(lambda m: m.text == "🔥 Свежие связки")
    async def kb_deals(message: Message) -> None:
        await arb_deals(message)

    @router.message(lambda m: m.text == "📋 Мои запросы")
    async def kb_list(message: Message) -> None:
        await arb_list(message)

    @router.message(lambda m: m.text == "📊 Моя СПП")
    async def kb_spp(message: Message) -> None:
        await arb_my_spp(message)

    @router.message(lambda m: m.text == "🏆 Топ категории")
    async def kb_top(message: Message) -> None:
        await arb_top_cat(message)

    # ── Entry from main menu (top-level button) ─────────────────
    @router.message(lambda m: m.text == "🎯 Арбитраж")
    async def kb_open_arb(message: Message) -> None:
        await arb_root(message)

    return router
