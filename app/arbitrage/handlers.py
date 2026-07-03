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

from app.arbitrage.auto_observer import AutoObserver
from app.arbitrage.repository import NM_LABELS, ArbitrageRepository
from app.arbitrage.scanner import ArbitrageScanner
from app.config import AppConfig
from app.handlers.common import ensure_allowed, remember_subscriber
from app.security import safe_md
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
    auto_observer: AutoObserver,
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
            "*Запросы:*\n"
            "• `/arb_add <фраза>` — добавить\n"
            "• `/arb_list` — мои запросы\n"
            "• `/arb_remove <id>` — отключить\n"
            "• `/arb_keywords <id> include=… exclude=…` — фильтр цвета/варианта\n"
            "\n*Качество (ground-truth):*\n"
            "• `/arb_mark <nm> <метка>` — пометить связку (`wrong_color`/…)\n"
            "\n*Наблюдения (СПП):*\n"
            "• `/arb_quickadd <nm> <моя_цена>` — авто-fetch публичной\n"
            "• `/arb_bulk` — массовый paste (см. формат внутри)\n"
            "• `/arb_observe <nm> <моя_цена> <публич>` — ручной ввод\n"
            "• `/arb_my_spp` — моя СПП по категориям\n"
            "• `/arb_top_cat` — топ категорий\n"
            "\n*Скан:*\n"
            "• `/arb_scan_now` — запустить сейчас\n"
            "• `/arb_deals` — свежие связки (24ч)\n"
            "\n💡 Записи закупок через /buy автоматически "
            "генерируют наблюдения.",
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
        except Exception:
            logger.exception("arb_add failed")
            await message.answer("❌ Не удалось добавить запрос. Подробности в логах.")

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
                    subj_info = f" → {safe_md(r['subject_name'])}"
            lines.append(
                f"#{r['id']} «{safe_md(r['query'])}»{subj_info}\n"
                f"   В каталоге: {r['last_found_count']}, последний скан: {last}"
            )
            # Hint: if cohort found but no observations in that category yet
            if r.get("subject_id") and r["last_found_count"] > 0:
                cat = await arb_repo.get_category_avg_spp(
                    r["subject_id"], days=30, min_samples=1,
                )
                if cat is None or cat.get("samples", 0) < 3:
                    hints.append(
                        f"⚠️ #{r['id']} «{safe_md(r['query'])}» — нет СПП-наблюдений "
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

    # ── /arb_keywords ───────────────────────────────────────────
    @router.message(Command("arb_keywords"))
    async def arb_keywords(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        tokens = (message.text or "").split()
        if len(tokens) < 2 or not tokens[1].isdigit():
            await message.answer(
                "Использование: `/arb_keywords <id> include=чёрн,серая exclude=восстановл`\n\n"
                "• `include=` — оставлять только товары с этими словами в названии (whitelist)\n"
                "• `exclude=` — убирать товары с этими словами\n"
                "• `/arb_keywords <id> clear` — снять фильтр\n\n"
                "Фильтр per-query: для «Станция Миди» задай цвета, для пылесосов оставь пустым.",
                parse_mode="Markdown",
            )
            return
        qid = int(tokens[1])
        queries = await arb_repo.list_queries(only_enabled=False)
        q = next((x for x in queries if x["id"] == qid), None)
        if q is None:
            await message.answer(f"❌ Запрос #{qid} не найден.")
            return

        rest = tokens[2:]
        if len(rest) == 1 and rest[0].lower() == "clear":
            await arb_repo.set_query_keywords(qid, include=None, exclude=None)
            await message.answer(f"✅ Фильтр запроса #{qid} снят.")
            return

        include = exclude = None  # None = не трогать существующее
        for tok in rest:
            if tok.startswith("include="):
                include = tok[len("include="):]
            elif tok.startswith("exclude="):
                exclude = tok[len("exclude="):]
        if include is None and exclude is None:
            await message.answer(
                "Не нашёл `include=` / `exclude=`. Пример: "
                "`/arb_keywords 2 include=чёрн,серая`",
                parse_mode="Markdown",
            )
            return

        inc = include if include is not None else q.get("include_keywords")
        exc = exclude if exclude is not None else q.get("exclude_keywords")
        await arb_repo.set_query_keywords(qid, include=inc, exclude=exc)
        await message.answer(
            f"✅ Фильтр запроса #{qid} «{safe_md(q['query'])}»:\n"
            f"• include: `{safe_md(inc or '—')}`\n"
            f"• exclude: `{safe_md(exc or '—')}`",
            parse_mode="Markdown",
        )

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
                lines.append(f"   {safe_md(name[:60])}")
        await message.answer("\n".join(lines), parse_mode="Markdown")

    # ── /arb_mark (ground-truth label) ──────────────────────────
    @router.message(Command("arb_mark"))
    async def arb_mark(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        tokens = (message.text or "").split(maxsplit=3)
        labels_hint = " | ".join(sorted(NM_LABELS))
        if len(tokens) < 3 or not tokens[1].isdigit() or tokens[2] not in NM_LABELS:
            await message.answer(
                "Использование: `/arb_mark <nm_id> <метка> [заметка]`\n\n"
                f"Метки: `{labels_hint}`\n\n"
                "Пример: `/arb_mark 304333036 wrong_color жёлтая, не моя`\n\n"
                "Метки дают честный знаменатель для измерения качества фильтра.",
                parse_mode="Markdown",
            )
            return
        nm_id = int(tokens[1])
        label = tokens[2]
        note = tokens[3].strip() if len(tokens) > 3 else None
        try:
            await arb_repo.add_nm_label(nm_id, label, note=note)
            await message.answer(f"✅ nm {nm_id} помечен: *{safe_md(label)}*", parse_mode="Markdown")
        except Exception:
            logger.exception("arb_mark failed")
            await message.answer("❌ Не удалось сохранить метку. Подробности в логах.")

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
                f"• {safe_md(c['subject_name'])}: AVG {c['avg_spp']:.1f}% "
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
                f"{i}. {safe_md(c['subject_name'])} — {c['avg_spp']:.1f}% (n={c['samples']})"
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
                "Где `публичная_цена` — цена продавца ДО WB-Скидки (СПП): как в кабинете "
                "продавца / listed конкурента.\n"
                "⚠️ НЕ бери цену со страницы товара — сайт показывает цену уже ПОСЛЕ СПП.",
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
        except Exception:
            logger.exception("arb_observe failed")
            await message.answer("❌ Не удалось записать наблюдение. Подробности в логах.")

    # ── /arb_quickadd ───────────────────────────────────────────
    @router.message(Command("arb_quickadd"))
    async def arb_quickadd(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        parts = (message.text or "").split()
        if len(parts) < 3:
            await message.answer(
                "Использование: `/arb_quickadd <nm_id> <моя_цена_на_checkout>`\n\n"
                "Бот сам найдёт публичную цену и посчитает СПП.\n"
                "Пример: `/arb_quickadd 876392996 10658`",
                parse_mode="Markdown",
            )
            return
        try:
            nm_id = int(parts[1])
            my_price = int(parts[2])
        except ValueError:
            await message.answer("Числа должны быть целыми. Пример: `/arb_quickadd 876392996 10658`",
                                  parse_mode="Markdown")
            return

        await message.answer("⏳ Запрашиваю публичную цену…")
        result = await auto_observer.observe(
            nm_id=nm_id, paid_price_rub=my_price,
            source="checkout_manual", note="quickadd",
        )
        if result.ok and result.wallet_only:
            await message.answer(
                f"✅ Наблюдение #{result.observation_id} записано (wallet-only).\n\n"
                f"nm: {nm_id}\n"
                f"Цена с сайта (уже ПОСЛЕ СПП): {result.public_price_rub:,}₽\n".replace(",", " ") +
                f"Моя цена: {result.paid_price_rub:,}₽\n".replace(",", " ") +
                f"Скидка кошелька: *{result.spp_percent:.1f}%*\n\n"
                f"⚠️ Чужой артикул: listed (цену ДО СПП) из публичного API не узнать, "
                f"поэтому наблюдение НЕ участвует в категорийной СПП. "
                f"Для неё используй `/arb_observe <nm> <моя_цена> <цена_продавца_до_СПП>`.",
                parse_mode="Markdown",
            )
        elif result.ok:
            await message.answer(
                f"✅ Наблюдение #{result.observation_id} записано.\n\n"
                f"nm: {nm_id}\n"
                f"Listed (до СПП, из своих продаж): {result.public_price_rub:,}₽\n".replace(",", " ") +
                f"Моя цена: {result.paid_price_rub:,}₽\n".replace(",", " ") +
                f"Моя СПП (композит): *{result.spp_percent:.1f}%* "
                f"({result.public_price_rub - result.paid_price_rub:,}₽ экономии)".replace(",", " "),
                parse_mode="Markdown",
            )
        else:
            reasons = {
                "invalid_nm_id": "Неверный nm_id",
                "wb_fetch_failed": "Не удалось получить данные WB (rate limit?)",
                "nm_not_found_on_wb": f"WB не нашёл товар nm={nm_id}",
                "public_price_zero": "WB вернул нулевую цену",
                "paid_outside_range": f"Моя цена {my_price}₽ выше публичной — проверь числа",
                "db_insert_failed": "Ошибка БД",
            }
            await message.answer(
                f"❌ {reasons.get(result.skipped_reason, result.skipped_reason)}"
            )

    # ── /arb_bulk ───────────────────────────────────────────────
    @router.message(Command("arb_bulk"))
    async def arb_bulk(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        text = (message.text or "")
        # Strip /arb_bulk prefix
        body = text.split("\n", 1)[1] if "\n" in text else ""
        body = body.strip()
        if not body:
            await message.answer(
                "📝 *Массовый ввод наблюдений*\n\n"
                "Использование: на новой строке после команды paste пары "
                "`<nm_id> <моя_цена>`, по одной паре на строку.\n\n"
                "Пример:\n"
                "```\n"
                "/arb_bulk\n"
                "876392996 10658\n"
                "260407160 8950\n"
                "193961961 11200\n"
                "```\n\n"
                "Бот сам найдёт публичные цены и запишет СПП для каждого.",
                parse_mode="Markdown",
            )
            return

        # Parse lines
        pairs: list[tuple[int, int]] = []
        errors: list[str] = []
        for i, line in enumerate(body.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            tokens = line.split()
            if len(tokens) < 2:
                errors.append(f"строка {i}: нужно 2 числа")
                continue
            try:
                nm = int(tokens[0])
                price = int(tokens[1])
                pairs.append((nm, price))
            except ValueError:
                errors.append(f"строка {i}: нечисловые значения")

        if not pairs:
            await message.answer(
                "❌ Не нашёл валидных пар. Каждая строка: `<nm_id> <цена>`",
                parse_mode="Markdown",
            )
            return

        await message.answer(f"⏳ Обрабатываю {len(pairs)} наблюдений…")
        ok_count = 0
        fail_count = 0
        lines: list[str] = []
        for nm, price in pairs:
            r = await auto_observer.observe(
                nm_id=nm, paid_price_rub=price,
                source="checkout_manual", note="bulk",
            )
            if r.ok:
                ok_count += 1
                spp_label = (
                    f"кошелёк {r.spp_percent:.1f}%, wallet-only"
                    if r.wallet_only else f"СПП {r.spp_percent:.1f}%"
                )
                lines.append(
                    f"✅ nm {nm}: публич {r.public_price_rub}₽ → моя {r.paid_price_rub}₽ "
                    f"({spp_label})"
                )
            else:
                fail_count += 1
                lines.append(f"❌ nm {nm}: {r.skipped_reason}")

        summary = [f"📦 Записано: {ok_count}/{len(pairs)} (ошибок: {fail_count})"]
        if errors:
            summary.append(f"⚠️ Parse ошибки: {len(errors)}")
        # Show first 15 lines
        await message.answer("\n".join(summary + [""] + lines[:15]))

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
                "✅ Скан завершён.",
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
                lines.append(f"• «{safe_md(q['query'])}» → {safe_md(subj)}: {found} товаров")
                if q.get("subject_id") and found > 0:
                    cat = await arb_repo.get_category_avg_spp(
                        q["subject_id"], days=30, min_samples=1,
                    )
                    if cat is None or cat.get("samples", 0) < 3:
                        need_obs.append(
                            f"• {safe_md(subj)} (нужно 3+ наблюдения)"
                        )
            if need_obs:
                lines.append("")
                lines.append("⚠️ *Категории без СПП-данных:*")
                lines.extend(need_obs)
                lines.append("")
                lines.append("Добавь `/arb_observe <nm> <моя_цена> <публич_цена>` "
                             "для 3+ товаров в этих категориях — сканер начнёт алертить.")
            await message.answer("\n".join(lines), parse_mode="Markdown")
        except Exception:
            logger.exception("arb_scan_now failed")
            await message.answer("❌ Ошибка сканера. Подробности в логах.")

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
