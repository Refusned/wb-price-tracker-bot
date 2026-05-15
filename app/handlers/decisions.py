from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from app.config import AppConfig
from app.storage.decision_snapshot_repository import DecisionSnapshotRepository
from app.storage.repositories import SubscriberRepository

from .common import ensure_allowed, remember_subscriber


def get_router(
    config: AppConfig,
    decision_snapshot_repo: DecisionSnapshotRepository,
    subscriber_repository: SubscriberRepository,
) -> Router:
    router = Router(name="decisions")

    @router.message(Command("decisions"))
    async def decisions_handler(message: Message, command: CommandObject) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        limit = _parse_limit(command.args, default=10)
        if limit is None:
            await message.answer("Использование: /decisions [N]")
            return

        rows = await decision_snapshot_repo.recent(limit=limit)
        if not rows:
            await message.answer("Снимков решений пока нет.")
            return

        lines = [f"Последние decision snapshots: {len(rows)}", ""]
        for row in rows:
            action = row["user_action"] or "-"
            alert = "✅" if bool(row["alert_sent"]) else "❌"
            lines.append(
                f"• {_fmt_dt(row['snapshot_at'])} "
                f"nm={row['nm_id']} "
                f"цена={_fmt_money(row['observed_price'])}₽ "
                f"маржа={_fmt_margin(row['observed_margin_estimate'])}% "
                f"alert={alert} "
                f"action={action}"
            )

        await message.answer("\n".join(lines))

    @router.message(Command("decision_stats"))
    async def decision_stats_handler(message: Message) -> None:
        if not await ensure_allowed(message, config):
            return
        await remember_subscriber(message, subscriber_repository)

        stats = await decision_snapshot_repo.distribution(days=30)
        by_action = stats["by_action"]
        top_nm = stats["by_nm_id_top10"][:3]

        lines = [
            "Decision snapshots за 30 дней",
            "",
            f"Всего: {stats['total']}",
            f"С алертом: {stats['alerted']}",
            "",
            "Действия:",
            f"bought: {by_action.get('bought', 0)}",
            f"ignored: {by_action.get('ignored', 0)}",
            f"too_late: {by_action.get('too_late', 0)}",
            f"без action: {by_action.get(None, 0)}",
            "",
            "Top nm_id:",
        ]

        if top_nm:
            for row in top_nm:
                lines.append(f"nm={row['nm_id']}: {row['count']}")
        else:
            lines.append("-")

        await message.answer("\n".join(lines))

    return router


def _parse_limit(args: str | None, default: int) -> int | None:
    raw = (args or "").strip()
    if not raw:
        return default
    if not raw.isdigit():
        return None
    value = int(raw)
    if value <= 0 or value > 100:
        return None
    return value


def _fmt_dt(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _fmt_money(value: float) -> str:
    return f"{float(value):.0f}"


def _fmt_margin(value: float | None) -> str:
    if value is None:
        return "-"
    text = f"{float(value):.1f}"
    return text[:-2] if text.endswith(".0") else text
