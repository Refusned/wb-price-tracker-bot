"""
Клиент WB Feedbacks & Questions API (feedbacks-api.wildberries.ru).

Читает неотвеченные отзывы/вопросы и публикует ответы. Требует токен WB
Seller API со scope «Вопросы и отзывы» (отдельный от статистики/контента).

⚠️ money/safety: answer_feedback / answer_question — МУТИРУЮЩИЕ. Они
публикуют ответ покупателю ПУБЛИЧНО и НЕОБРАТИМО. В тестах их НИКОГДА не
зовут с реальным ключом (только фейк-сессия); на проде — лишь через
FeedbackResponder под флагом FEEDBACK_AUTO_REPLY_ENABLED. Для проверки
формы запросов есть sandbox-хост (sandbox=True).

Формы подтверждены по офиц. доке WB (user-communication):
    GET   /api/v1/feedbacks?isAnswered=false&take&skip&order  -> data.feedbacks[]
    PATCH /api/v1/feedbacks   {"id","text"}                    -> 204
    GET   /api/v1/questions?isAnswered=false&take&skip&order   -> data.questions[]
    PATCH /api/v1/questions   {"id","answer":{"text"},"state":"wbRu"}
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp


_PROD_BASE = "https://feedbacks-api.wildberries.ru"
_SANDBOX_BASE = "https://feedbacks-api-sandbox.wildberries.ru"
# Категория «Вопросы и отзывы»: 3 запроса/сек (burst 6). Держим консервативно.
_MIN_INTERVAL = 0.4
# WB: текст ответа на отзыв — [2 .. 5000] символов.
ANSWER_MIN_LEN = 2
ANSWER_MAX_LEN = 5000


class FeedbacksApiError(Exception):
    """WB Feedbacks API недоступен или ответил ошибкой после запроса."""


@dataclass(slots=True)
class Feedback:
    id: str
    text: str
    rating: int          # productValuation, 1..5 (0 — если WB не прислал)
    created_date: str
    nm_id: int
    product_name: str
    user_name: str
    pros: str = ""
    cons: str = ""
    answered: bool = False   # уже есть ответ (WB поле answer != null)


@dataclass(slots=True)
class Question:
    id: str
    text: str
    created_date: str
    nm_id: int
    product_name: str
    answered: bool = False   # уже есть ответ (WB поле answer != null)


class WBFeedbacksClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        sandbox: bool = False,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._base = _SANDBOX_BASE if sandbox else _PROD_BASE
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._lock = asyncio.Lock()
        self._last_ts = 0.0
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": self._api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def _throttle(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_ts
            if elapsed < _MIN_INTERVAL:
                await asyncio.sleep(_MIN_INTERVAL - elapsed)
            self._last_ts = time.monotonic()

    # ---------- reads ----------

    async def get_unanswered_feedbacks(self, *, take: int = 100, skip: int = 0) -> list[Feedback]:
        data = await self._get(
            "/api/v1/feedbacks",
            {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"},
        )
        return [self._parse_feedback(f) for f in self._extract_list(data, "feedbacks")]

    async def get_unanswered_questions(self, *, take: int = 100, skip: int = 0) -> list[Question]:
        data = await self._get(
            "/api/v1/questions",
            {"isAnswered": "false", "take": take, "skip": skip, "order": "dateDesc"},
        )
        return [self._parse_question(q) for q in self._extract_list(data, "questions")]

    # ---------- writes (МУТИРУЮЩИЕ — публикуют ответ покупателю) ----------

    async def answer_feedback(self, feedback_id: str, text: str) -> None:
        """Опубликовать ответ на отзыв. PATCH /api/v1/feedbacks {id,text} -> 204."""
        await self._patch("/api/v1/feedbacks", {"id": feedback_id, "text": text})

    async def answer_question(self, question_id: str, text: str) -> None:
        """Опубликовать ответ на вопрос. state=wbRu публикует на wb.ru."""
        await self._patch(
            "/api/v1/questions",
            {"id": question_id, "answer": {"text": text}, "state": "wbRu"},
        )

    # ---------- HTTP ----------

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        await self._throttle()
        url = f"{self._base}{path}"
        try:
            async with self._session.get(
                url, params=params, headers=self._headers, timeout=self._timeout,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise FeedbacksApiError(f"GET {path} -> HTTP {resp.status}: {body}")
                return await resp.json()
        except aiohttp.ClientError as exc:
            raise FeedbacksApiError(f"GET {path} failed: {exc}") from exc

    async def _patch(self, path: str, body: dict[str, Any]) -> None:
        await self._throttle()
        url = f"{self._base}{path}"
        try:
            async with self._session.patch(
                url, json=body, headers=self._headers, timeout=self._timeout,
            ) as resp:
                # WB отвечает 204 (отзыв) или 200 (вопрос). Всё прочее — ошибка.
                if resp.status not in (200, 204):
                    txt = (await resp.text())[:300]
                    raise FeedbacksApiError(f"PATCH {path} -> HTTP {resp.status}: {txt}")
        except aiohttp.ClientError as exc:
            raise FeedbacksApiError(f"PATCH {path} failed: {exc}") from exc

    # ---------- parsing ----------

    @staticmethod
    def _extract_list(data: Any, key: str) -> list[dict[str, Any]]:
        if not isinstance(data, dict):
            return []
        payload = data.get("data")
        if not isinstance(payload, dict):
            return []
        items = payload.get(key)
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    @staticmethod
    def _parse_feedback(f: dict[str, Any]) -> Feedback:
        pd = f.get("productDetails") or {}
        return Feedback(
            id=str(f.get("id", "")),
            text=str(f.get("text") or ""),
            rating=int(f.get("productValuation") or 0),
            created_date=str(f.get("createdDate") or ""),
            nm_id=int(pd.get("nmId") or 0),
            product_name=str(pd.get("productName") or ""),
            user_name=str(f.get("userName") or ""),
            pros=str(f.get("pros") or ""),
            cons=str(f.get("cons") or ""),
            answered=bool(f.get("answer")),
        )

    @staticmethod
    def _parse_question(q: dict[str, Any]) -> Question:
        pd = q.get("productDetails") or {}
        return Question(
            id=str(q.get("id", "")),
            text=str(q.get("text") or ""),
            created_date=str(q.get("createdDate") or ""),
            nm_id=int(pd.get("nmId") or 0),
            product_name=str(pd.get("productName") or ""),
            answered=bool(q.get("answer")),
        )
