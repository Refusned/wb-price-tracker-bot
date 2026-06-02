"""
Чистые помощники публикации ответа покупателю WB — общие для автопостера
(FeedbackResponder, Фаза 1) и ручного подтверждения из диалога-агента (Фаза 3).

MS-7: единственный источник money-safety-критичной логики «что и как можно
опубликовать» — контент-гейт + финализация + идемпотентная последовательность.
Дублировать её в двух местах нельзя (иначе расхождение = публичный косяк).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from app.storage.feedback_reply_repository import FeedbackReplyRepository
from app.wb.feedbacks_client import ANSWER_MAX_LEN, ANSWER_MIN_LEN, FeedbacksApiError

_logger = logging.getLogger("feedback_posting")

# Контент-гейт: ответ покупателю НЕ должен содержать ссылок/почты/телефонов.
_URL_RE = re.compile(r"https?://|\bwww\.", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_RE = re.compile(r"(?:\+?\d[\s\-()]*){10,}")  # 10+ цифр подряд = телефон


def content_gate(text: str) -> bool:
    """True, если текст БЕЗОПАСЕН к публикации (нет ссылок/почты/телефона).

    LLM-вывод уходит покупателю необратимо; промпт это запрещает, но ловим и
    здесь — в т.ч. на случай prompt-injection из текста отзыва/запроса.
    """
    return not (_URL_RE.search(text) or _EMAIL_RE.search(text) or _PHONE_RE.search(text))


def finalize_answer(draft: str, signature: str = "") -> str | None:
    """Привести черновик к публикуемому виду либо вернуть None (НЕ публиковать).

    Снимает обрамляющие кавычки (только однозначную пару, не повреждая текст с
    внутренними кавычками), проверяет мин. длину, прогоняет контент-гейт,
    обрезает ДО подписи под лимит WB и добавляет доверенную подпись владельца.
    """
    text = (draft or "").strip()
    pairs = {"«": "»", "“": "”"}
    if (
        len(text) >= 2
        and text[0] in pairs
        and text[-1] == pairs[text[0]]
        and pairs[text[0]] not in text[1:-1]
    ):
        text = text[1:-1].strip()
    if len(text) < ANSWER_MIN_LEN:
        return None
    if not content_gate(text):
        return None
    sig = f"\n\n{signature}" if signature else ""
    budget = ANSWER_MAX_LEN - len(sig)
    if len(text) > budget:
        text = text[:budget].rstrip()
    return f"{text}{sig}"


async def _safe_record(repo: FeedbackReplyRepository, **kwargs: object) -> None:
    """Запись в журнал не должна ронять вызывающего."""
    try:
        await repo.record(**kwargs)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        _logger.warning("feedback_replies record failed: %s", exc)


async def post_reply_idempotent(
    repo: FeedbackReplyRepository,
    *,
    kind: str,
    feedback_id: str,
    original_text: str,
    answer: str,
    publish: Callable[[str], Awaitable[None]],
    nm_id: int | None = None,
    product_name: str | None = None,
    rating: int | None = None,
) -> tuple[bool, str]:
    """Идемпотентная публикация уже финализированного ответа: резерв(pending) →
    publish → posted. Возвращает (ok, status).

    НЕ генерирует текст и НЕ применяет контент-гейт — answer должен быть уже
    прогнан через finalize_answer. НЕ проверяет is_handled — это делает
    вызывающий ДО (MS-3). Не бросает на сбое публикации: пишет 'failed'.
    """
    # резерв перед публикацией: если не записался — НЕ публикуем (гарантия от дубля)
    try:
        await repo.record(
            kind=kind, feedback_id=feedback_id, original_text=original_text,
            answer_text=answer, status="pending",
            nm_id=nm_id, product_name=product_name, rating=rating,
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning("reserve failed for %s %s: %s — НЕ публикую", kind, feedback_id, exc)
        return False, "reserve_failed"

    # публикация (МУТАЦИЯ — публичный необратимый ответ покупателю)
    try:
        await publish(answer)
    except FeedbacksApiError as exc:
        # откат резерва → failed; попытку уже посчитал резерв (increment=False)
        await _safe_record(
            repo, kind=kind, feedback_id=feedback_id, original_text=original_text,
            answer_text=answer, status="failed", nm_id=nm_id, product_name=product_name,
            rating=rating, error=str(exc)[:300], increment=False,
        )
        return False, "publish_failed"

    # подтверждаем posted (increment=False); pending уже не даст опубликовать дважды
    await _safe_record(
        repo, kind=kind, feedback_id=feedback_id, original_text=original_text,
        answer_text=answer, status="posted", nm_id=nm_id, product_name=product_name,
        rating=rating, error=None, increment=False,
    )
    return True, "posted"
