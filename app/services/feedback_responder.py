"""
Фаза 1: автоответы на отзывы и вопросы покупателей WB через LLM.

Один цикл run_once():
    poll неотвеченных отзывов/вопросов → для каждого ещё не обработанного:
        LLM-черновик → sanity-check → публикация в WB → запись в журнал → DM владельцу.

Любой сбой (LLM упала / пустой ответ / WB вернул ошибку) → НИЧЕГО не
публикуется, в журнал пишется 'failed'/'skipped', цикл идёт дальше. Дважды на
один отзыв не отвечаем (журнал + WB-фильтр isAnswered=false).

Автопостинг (по решению владельца — без подтверждения перед публикацией).
Безопасность на уровне промпта: модели ЗАПРЕЩЕНО выдумывать сроки/гарантии/
возвраты — это дешёвая защита от галлюцинаций, совместимая с автопостингом.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import Bot

from app.config import AppConfig
from app.llm.client import LLMClient, LLMError
from app.storage.feedback_reply_repository import FeedbackReplyRepository
from app.storage.repositories import SubscriberRepository
from app.wb.feedbacks_client import (
    ANSWER_MAX_LEN,
    ANSWER_MIN_LEN,
    Feedback,
    FeedbacksApiError,
    Question,
    WBFeedbacksClient,
)


_SYSTEM_PROMPT = (
    "Ты — представитель службы заботы о покупателях интернет-магазина на "
    "Wildberries. Отвечаешь на отзывы и вопросы покупателей.\n"
    "Правила:\n"
    "- Только на русском, на «вы», дружелюбно и тепло, без канцелярита и воды.\n"
    "- 1–3 коротких предложения.\n"
    "- За отзыв — поблагодари; на вопрос — ответь конкретно и по делу.\n"
    "- НИКОГДА не обещай того, чего не знаешь: не выдумывай сроки, гарантии, "
    "возвраты денег, скидки, характеристики или наличие. Если данных нет — "
    "отвечай общими словами и предложи написать в поддержку магазина.\n"
    "- На негатив реагируй спокойно: извинись за неудобства, прояви заботу, "
    "без споров и оправданий.\n"
    "- Не упоминай, что ты ИИ. Без ссылок. Не проси личные данные.\n"
    "Верни ТОЛЬКО текст ответа покупателю — без кавычек, пояснений и префиксов."
)


class FeedbackResponder:
    def __init__(
        self,
        *,
        feedbacks_client: WBFeedbacksClient,
        llm_client: LLMClient,
        reply_repo: FeedbackReplyRepository,
        subscriber_repository: SubscriberRepository,
        bot: Bot,
        config: AppConfig,
    ) -> None:
        self._fb = feedbacks_client
        self._llm = llm_client
        self._repo = reply_repo
        self._subscribers = subscriber_repository
        self._bot = bot
        self._signature = (config.feedback_signature or "").strip()
        self._max_per_cycle = max(1, getattr(config, "feedback_max_per_cycle", 10))
        self._logger = logging.getLogger(self.__class__.__name__)

    async def run_once(self) -> dict[str, int]:
        stats = {"feedbacks": 0, "questions": 0, "posted": 0, "failed": 0}
        try:
            feedbacks = await self._fb.get_unanswered_feedbacks()
            questions = await self._fb.get_unanswered_questions()
        except FeedbacksApiError as exc:
            self._logger.warning("feedbacks poll failed: %s", exc)
            return stats

        stats["feedbacks"] = len(feedbacks)
        stats["questions"] = len(questions)

        budget = self._max_per_cycle
        for fb in feedbacks:
            if budget <= 0:
                break
            if not fb.id or await self._repo.is_handled("feedback", fb.id):
                continue
            budget -= 1
            ok = await self._handle_feedback(fb)
            stats["posted" if ok else "failed"] += 1

        for q in questions:
            if budget <= 0:
                break
            if not q.id or await self._repo.is_handled("question", q.id):
                continue
            budget -= 1
            ok = await self._handle_question(q)
            stats["posted" if ok else "failed"] += 1

        return stats

    # ---------- per-item ----------

    async def _handle_feedback(self, fb: Feedback) -> bool:
        prompt = (
            f"Товар: {fb.product_name or '—'}\n"
            f"Оценка: {fb.rating}/5\n"
            f"Отзыв покупателя: {fb.text or '(без текста)'}"
        )
        if fb.pros:
            prompt += f"\nДостоинства: {fb.pros}"
        if fb.cons:
            prompt += f"\nНедостатки: {fb.cons}"
        prompt += "\nНапиши ответ покупателю."
        return await self._generate_and_post(
            kind="feedback",
            item_id=fb.id,
            prompt=prompt,
            original_text=fb.text,
            nm_id=fb.nm_id,
            product_name=fb.product_name,
            rating=fb.rating,
            publish=lambda text: self._fb.answer_feedback(fb.id, text),
        )

    async def _handle_question(self, q: Question) -> bool:
        prompt = (
            f"Товар: {q.product_name or '—'}\n"
            f"Вопрос покупателя: {q.text or '(без текста)'}\n"
            "Напиши ответ покупателю."
        )
        return await self._generate_and_post(
            kind="question",
            item_id=q.id,
            prompt=prompt,
            original_text=q.text,
            nm_id=q.nm_id,
            product_name=q.product_name,
            rating=None,
            publish=lambda text: self._fb.answer_question(q.id, text),
        )

    async def _generate_and_post(
        self,
        *,
        kind: str,
        item_id: str,
        prompt: str,
        original_text: str,
        nm_id: int,
        product_name: str,
        rating: int | None,
        publish: Callable[[str], Awaitable[None]],
    ) -> bool:
        # 1) LLM-черновик
        try:
            draft = await self._llm.generate(system=_SYSTEM_PROMPT, user=prompt, num_predict=400)
        except LLMError as exc:
            self._logger.warning("LLM failed for %s %s: %s", kind, item_id, exc)
            await self._record(kind, item_id, original_text, "", "failed",
                               nm_id, product_name, rating, str(exc)[:300])
            return False

        # 2) sanity-гейт — пустой/битый не публикуем
        answer = self._finalize(draft)
        if answer is None:
            self._logger.warning("sanity-reject for %s %s", kind, item_id)
            await self._record(kind, item_id, original_text, draft[:500], "skipped",
                               nm_id, product_name, rating, "sanity-reject")
            return False

        # 3) публикация в WB (МУТАЦИЯ)
        try:
            await publish(answer)
        except FeedbacksApiError as exc:
            self._logger.warning("WB publish failed for %s %s: %s", kind, item_id, exc)
            await self._record(kind, item_id, original_text, answer, "failed",
                               nm_id, product_name, rating, str(exc)[:300])
            return False

        # 4) журнал + DM владельцу
        await self._record(kind, item_id, original_text, answer, "posted",
                           nm_id, product_name, rating, None)
        await self._notify_owner(kind, rating, product_name, original_text, answer)
        self._logger.info("Auto-replied to %s %s (nm=%s)", kind, item_id, nm_id)
        return True

    def _finalize(self, draft: str) -> str | None:
        text = (draft or "").strip()
        # снять обрамляющие кавычки, если модель их добавила
        if len(text) >= 2 and text[0] in "«\"'" and text[-1] in "»\"'":
            text = text[1:-1].strip()
        if self._signature:
            text = f"{text}\n\n{self._signature}"
        if len(text) < ANSWER_MIN_LEN:
            return None
        if len(text) > ANSWER_MAX_LEN:
            text = text[:ANSWER_MAX_LEN].rstrip()
        return text

    async def _record(
        self,
        kind: str,
        feedback_id: str,
        original_text: str,
        answer_text: str,
        status: str,
        nm_id: int,
        product_name: str,
        rating: int | None,
        error: str | None,
    ) -> None:
        try:
            await self._repo.record(
                kind=kind,
                feedback_id=feedback_id,
                original_text=original_text,
                answer_text=answer_text,
                status=status,
                nm_id=nm_id,
                product_name=product_name,
                rating=rating,
                error=error,
            )
        except Exception as exc:  # журнал не должен ронять цикл
            self._logger.warning("feedback_replies record failed: %s", exc)

    async def _notify_owner(
        self,
        kind: str,
        rating: int | None,
        product_name: str,
        original_text: str,
        answer: str,
    ) -> None:
        try:
            chat_ids = await self._subscribers.list_active_chat_ids()
        except Exception:
            return
        label = "отзыв" if kind == "feedback" else "вопрос"
        stars = f" {'⭐' * rating}" if (kind == "feedback" and rating) else ""
        text = (
            f"🤖 Ответил на {label}{stars} «{product_name or '—'}»\n\n"
            f"Покупатель: {(original_text or '(без текста)')[:300]}\n\n"
            f"Ответ: {answer[:500]}"
        )
        for chat_id in chat_ids:
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
            except Exception as exc:
                self._logger.warning("owner DM failed chat=%s: %s", chat_id, exc)
