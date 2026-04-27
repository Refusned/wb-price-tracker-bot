from __future__ import annotations

from datetime import datetime

from app.services.margin_calculator import MarginResult
from app.storage.models import Item, PriceDropEvent


def format_price_rub(value: float | int) -> str:
    numeric = float(value)
    if numeric.is_integer():
        return f"{int(numeric):,}".replace(",", " ") + " ₽"
    return f"{numeric:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def format_iso_datetime(iso_value: str | None) -> str:
    if not iso_value:
        return "нет данных"
    try:
        dt = datetime.fromisoformat(iso_value)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso_value


def _estimate_wb_site_price(api_price: float) -> float:
    """WB website shows ~6% lower price than API (WB's own discount)."""
    return round(api_price * 0.94, 0)


def build_top10_message(
    *,
    query: str,
    min_price_rub: int,
    updated_at_iso: str | None,
    items: list[Item],
) -> str:
    header = [
        f"ТОП-{len(items)} \"{query}\" на WB (в наличии, от {format_price_rub(min_price_rub)})",
        f"Обновлено: {format_iso_datetime(updated_at_iso)}",
        "",
    ]

    body: list[str] = []
    for idx, item in enumerate(items, start=1):
        site_price = _estimate_wb_site_price(item.price_rub)
        body.append(f"{idx}) {item.name}")
        body.append(f"~{format_price_rub(site_price)} на сайте (API: {format_price_rub(item.price_rub)})")

        if item.stock_qty is not None:
            body.append(f"В наличии: {item.stock_qty} шт.")
        else:
            body.append("В наличии: да")

        body.append(f"Артикул: {item.nm_id}")
        body.append(f"Ссылка: {item.url}")
        body.append("")

    return "\n".join(header + body).strip()


def build_price_drop_alert_message(
    *,
    query: str,
    updated_at_iso: str,
    event: PriceDropEvent,
    margin: MarginResult | None = None,
    batch_size: int = 25,
) -> str:
    rank_str = f"#{event.top_rank} в топе по цене" if event.top_rank else ""
    header = f"🔥 Снижение цены: \"{query}\""
    if rank_str:
        header += f" ({rank_str})"

    lines = [
        header,
        f"Товар: {event.name}",
        (
            f"Цена сейчас: {format_price_rub(event.new_price_rub)} "
            f"(было 5 мин назад: {format_price_rub(event.previous_price_rub)})"
        ),
        f"Падение: -{event.drop_percent:.2f}%",
    ]

    if event.stock_qty is not None:
        lines.append(f"В наличии: {event.stock_qty} шт.")
    else:
        lines.append("В наличии: да")

    if margin is not None:
        lines.append("")
        lines.append(f"С СПП: {format_price_rub(margin.buy_price_with_spp)}")
        lines.append(f"Прибыль: {format_price_rub(margin.profit_per_unit)}/шт ({margin.margin_percent}%)")
        lines.append(f"На {batch_size} шт: {format_price_rub(margin.profit_per_unit * batch_size)}")
        if not margin.is_profitable:
            lines.append("Ниже порога маржи")

    lines.extend(
        [
            "",
            f"Артикул: {event.nm_id}",
            f"Ссылка: {event.url}",
            f"Обновлено: {format_iso_datetime(updated_at_iso)}",
        ]
    )

    return "\n".join(lines)


def format_margin_result(result: MarginResult, *, batch_size: int = 25) -> str:
    lines = [
        "Расчёт маржи:",
        f"Закупка: {format_price_rub(result.buy_price)}",
        f"С СПП: {format_price_rub(result.buy_price_with_spp)}",
        f"Продажа: {format_price_rub(result.sell_price)}",
        "",
        "Расходы:",
        f"  Комиссия WB: {format_price_rub(result.wb_commission)}",
        f"  Логистика: {format_price_rub(result.logistics)}",
        f"  Хранение (14д): {format_price_rub(result.storage)}",
        f"  Возвраты: {format_price_rub(result.return_cost)}",
        "",
        f"Чистая прибыль: {format_price_rub(result.profit_per_unit)}/шт",
        f"Маржа: {result.margin_percent}%",
        f"ROI: {result.roi_percent}%",
        f"На {batch_size} шт: {format_price_rub(result.profit_per_unit * batch_size)}",
    ]
    if result.is_profitable:
        lines.append("Сделка выгодная")
    else:
        lines.append("Ниже порога маржи")
    return "\n".join(lines)
