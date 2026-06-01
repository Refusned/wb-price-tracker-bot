"""
LLM-клиент для Ollama Cloud (и любого Ollama-API-совместимого эндпоинта).

Говорит на нативном chat-API Ollama: POST {base_url}/api/chat с телом
{"model", "messages", "stream": false}. Для Ollama Cloud base_url =
https://ollama.com, авторизация — Bearer-ключ (ollama.com/settings/keys).
Для локального Ollama поставь LLM_BASE_URL=http://localhost:11434.

Сознательно тонкий: один метод generate(). Используется и автоответами на
отзывы (Фаза 1), и будущим советником по кабинету (Фаза 2) — общая основа.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp


class LLMError(Exception):
    """LLM недоступна / вернула пустой ответ после всех ретраев.

    Бросается намеренно (вместо возврата ""), чтобы вызывающий код
    (FeedbackResponder) НЕ опубликовал покупателю пустой/битый ответ:
    при LLMError отзыв просто пропускается до следующего цикла.
    """


class LLMClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        api_key: str,
        base_url: str = "https://ollama.com",
        model: str = "deepseek-v4-pro",
        timeout_seconds: float = 60.0,
        retries: int = 2,
        backoff_seconds: float = 2.0,
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._retries = max(1, retries)
        self._backoff = backoff_seconds
        self._logger = logging.getLogger(self.__class__.__name__)

    @property
    def model(self) -> str:
        return self._model

    @property
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def generate(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.3,
        num_predict: int | None = None,
        think: bool | None = None,
    ) -> str:
        """Однократный chat-запрос. Возвращает текст ассистента (stripped).

        think: для thinking-моделей (deepseek-v4-pro и т.п.). False — отключает
        «размышление», чтобы весь num_predict шёл в content. Если оставить
        thinking включённым, на длинных промптах бюджет токенов уходит в
        message.thinking и content приходит ПУСТЫМ. None — поле не отправляем.

        Raises:
            LLMError: если все попытки провалились или ответ пуст.
        """
        options: dict[str, Any] = {"temperature": temperature}
        if num_predict is not None:
            options["num_predict"] = num_predict
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options,
        }
        if think is not None:
            payload["think"] = think
        url = f"{self._base_url}/api/chat"

        last_exc: Exception | None = None
        for attempt in range(self._retries):
            is_last = attempt == self._retries - 1
            try:
                async with self._session.post(
                    url, json=payload, headers=self._headers, timeout=self._timeout,
                ) as resp:
                    if resp.status != 200:
                        body = (await resp.text())[:300]
                        last_exc = LLMError(f"HTTP {resp.status}: {body}")
                        self._logger.warning(
                            "LLM HTTP %s (attempt %d/%d): %s",
                            resp.status, attempt + 1, self._retries, body,
                        )
                    else:
                        content = self._extract_content(await resp.json())
                        if content:
                            return content
                        last_exc = LLMError("LLM вернула пустой content")
                        self._logger.warning(
                            "LLM empty content (attempt %d/%d)", attempt + 1, self._retries,
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                self._logger.warning(
                    "LLM request failed (attempt %d/%d): %s",
                    attempt + 1, self._retries, exc,
                )

            if not is_last:
                await asyncio.sleep(self._backoff * (attempt + 1))

        raise LLMError(f"LLM не ответила после {self._retries} попыток") from last_exc

    @staticmethod
    def _extract_content(data: Any) -> str:
        """Достать текст ассистента из ответа Ollama /api/chat.

        Нативная форма: {"message": {"role": "assistant", "content": "..."}}.
        У 'thinking'-моделей рассуждение лежит в message.thinking; финальный
        ответ — в message.content, берём только его.
        """
        if not isinstance(data, dict):
            return ""
        message = data.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()
        return ""
