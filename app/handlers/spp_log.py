from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import AppConfig
from app.storage.personal_spp_repository import PersonalSppRepository

from .common import ensure_allowed


def get_router(config: AppConfig, personal_spp_repo: PersonalSppRepository) -> Router:
    router = Router(name="spp_log")

    @router.message(Command("setspp_log"))
    async def set_spp_log_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return

        args = (command.args or "").strip()
        try:
            percent = float(args.replace(",", "."))
        except ValueError:
            await message.answer("Использование: /setspp_log <0-100>")
            return

        if percent < 0.0 or percent > 100.0:
            await message.answer("Использование: /setspp_log <0-100>")
            return

        await personal_spp_repo.log_snapshot(percent, source="manual_command")

        today = datetime.now(timezone.utc).date()
        today_rows = await personal_spp_repo.history(days=1)
        n_today = sum(
            1
            for row in today_rows
            if _parse_iso(row["snapshot_at"]).date() == today
        )
        trend = await personal_spp_repo.trend(window_days=7)
        mean = trend["mean"] if trend is not None else percent

        await message.answer(
            f"✅ СПП {_fmt(percent)}% записан. "
            f"Сегодня: {n_today} записей. "
            f"Последняя 7-дневная средняя: {_fmt(mean)}%."
        )

    @router.message(Command("spp_history"))
    async def spp_history_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return

        days = 30
        args = (command.args or "").strip()
        if args:
            try:
                days = int(args)
            except ValueError:
                await message.answer("Использование: /spp_history [days]")
                return
            if days <= 0 or days > 365:
                await message.answer("Использование: /spp_history [days]")
                return

        rows = await personal_spp_repo.history(days=days)
        if not rows:
            await message.answer(f"За последние {days} дней записей СПП нет.")
            return

        lines = [
            "| Дата | Категория | СПП | Источник |",
            "|---|---|---:|---|",
        ]
        for row in rows:
            date = row["snapshot_at"][:10]
            lines.append(
                f"| {date} | {row['category']} | {_fmt(row['spp_percent'])}% | {row['source']} |"
            )
        await message.answer("\n".join(lines), parse_mode="Markdown")

    @router.message(Command("spp_trend"))
    async def spp_trend_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return

        trend = await personal_spp_repo.trend(window_days=7)
        if trend is None:
            await message.answer("Нет данных СПП для 7-дневного тренда.")
            return

        history = list(reversed(await personal_spp_repo.history(days=7)))
        values = [float(row["spp_percent"]) for row in history]
        sparkline = _ascii_sparkline(values)
        warning = ""
        if trend["drop_pct_vs_window"] > 15.0:
            warning = "\n⚠️ Sustained drop — re-evaluate buy strategy"

        await message.answer(
            "СПП 7-дневный тренд: "
            f"{_fmt(trend['current'])}% "
            f"(mean {_fmt(trend['mean'])}%, "
            f"range {_fmt(trend['min'])}-{_fmt(trend['max'])}). "
            f"Drop vs mean: {_fmt(trend['drop_pct_vs_window'])}%\n"
            f"{sparkline}"
            f"{warning}"
        )

    return router


def _parse_iso(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _ascii_sparkline(values: list[float]) -> str:
    if not values:
        return ""
    chars = "._-~=+*#"
    low = min(values)
    high = max(values)
    if high == low:
        return chars[len(chars) // 2] * len(values)
    return "".join(
        chars[round((value - low) / (high - low) * (len(chars) - 1))]
        for value in values
    )
