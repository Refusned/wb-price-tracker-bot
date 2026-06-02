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
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp


class LLMError(Exception):
    """LLM недоступна / вернула пустой ответ после всех ретраев.

    Бросается намеренно (вместо возврата ""), чтобы вызывающий код
    (FeedbackResponder) НЕ опубликовал покупателю пустой/битый ответ:
    при LLMError отзыв просто пропускается до следующего цикла.
    """


@dataclass(slots=True)
class ToolCall:
    """Один вызов инструмента, запрошенный моделью.

    raw — исходный элемент tool_calls от сервера (с index/type/function);
    кладётся обратно в историю дословно. name/arguments — распарсенные поля
    для исполнения; arguments всегда dict (см. _parse_arguments).
    """
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any]


@dataclass(slots=True)
class ChatResult:
    """Результат одного chat-обращения (один ход модели).

    content — текст ассистента (может быть "" при наличии tool_calls).
    tool_calls — запрошенные вызовы (пусто → финальный ответ).
    raw_message — ПОЛНОЕ assistant-сообщение как пришло от сервера; оркестратор
    кладёт его в историю дословно (Ollama требует index/type в tool_calls на
    следующем ходу).
    """
    content: str
    tool_calls: list[ToolCall]
    raw_message: dict[str, Any]


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

    async def _post_chat_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Одна попытка POST {base_url}/api/chat.

        Возвращает распарсенный JSON при 200; бросает LLMError на не-200 или
        транспортной ошибке. Содержимое (content/tool_calls) НЕ интерпретирует —
        это делают generate()/chat(). Общий примитив, чтобы ретраи не дублировались.
        """
        url = f"{self._base_url}/api/chat"
        try:
            async with self._session.post(
                url, json=payload, headers=self._headers, timeout=self._timeout,
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    raise LLMError(f"HTTP {resp.status}: {body}")
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise LLMError(f"LLM transport error: {exc}") from exc

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
        payload: dict[str, Any] = {
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

        last_exc: Exception | None = None
        for attempt in range(self._retries):
            is_last = attempt == self._retries - 1
            try:
                data = await self._post_chat_once(payload)
            except LLMError as exc:
                last_exc = exc
                self._logger.warning(
                    "LLM request failed (attempt %d/%d): %s",
                    attempt + 1, self._retries, exc,
                )
            else:
                content = self._extract_content(data)
                if content:
                    return content
                last_exc = LLMError("LLM вернула пустой content")
                self._logger.warning(
                    "LLM empty content (attempt %d/%d)", attempt + 1, self._retries,
                )
            if not is_last:
                await asyncio.sleep(self._backoff * (attempt + 1))

        raise LLMError(f"LLM не ответила после {self._retries} попыток") from last_exc

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.3,
        num_predict: int | None = None,
        think: bool | None = None,
    ) -> ChatResult:
        """Один ход агента: messages (+ tools) → ChatResult.

        В ОТЛИЧИЕ от generate(): НЕ бросает на пустом content — пустой content
        при наличии tool_calls это нормальная просьба вызвать инструмент (а на
        thinking-моделях content на tool-ходе всегда пуст). Ретраит только
        сетевые сбои/не-200; сборка истории и role:'tool' — задача вызывающего.
        """
        options: dict[str, Any] = {"temperature": temperature}
        if num_predict is not None:
            options["num_predict"] = num_predict
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": options,
        }
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think

        last_exc: Exception | None = None
        for attempt in range(self._retries):
            is_last = attempt == self._retries - 1
            try:
                data = await self._post_chat_once(payload)
            except LLMError as exc:
                last_exc = exc
                self._logger.warning(
                    "LLM chat failed (attempt %d/%d): %s",
                    attempt + 1, self._retries, exc,
                )
                if not is_last:
                    await asyncio.sleep(self._backoff * (attempt + 1))
                continue
            return self._extract_chat_result(data)

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

    @staticmethod
    def _parse_arguments(raw_args: Any) -> dict[str, Any]:
        """arguments из tool_call → всегда dict.

        Ollama обычно отдаёт объект ({"city": "Москва"}); некоторые модели/прокси
        кладут JSON-строкой. Парсим обе формы; на мусоре — пустой dict (тул
        отработает на дефолтах, цикл не упадёт).
        """
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            s = raw_args.strip()
            if not s:
                return {}
            try:
                parsed = json.loads(s)
            except (ValueError, TypeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _extract_chat_result(cls, data: Any) -> ChatResult:
        """Распарсить ответ /api/chat в ChatResult. Толерантен к мусору."""
        message = data.get("message") if isinstance(data, dict) else None
        if not isinstance(message, dict):
            return ChatResult(content="", tool_calls=[],
                              raw_message={"role": "assistant", "content": ""})
        content = message.get("content")
        content = content.strip() if isinstance(content, str) else ""
        tool_calls: list[ToolCall] = []
        raw_tcs = message.get("tool_calls")
        if isinstance(raw_tcs, list):
            for tc in raw_tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if not isinstance(name, str) or not name:
                    continue
                tool_calls.append(ToolCall(
                    name=name,
                    arguments=cls._parse_arguments(fn.get("arguments")),
                    raw=tc,
                ))
        return ChatResult(content=content, tool_calls=tool_calls, raw_message=message)
