"""Безопасность callback-кнопок и текста для Telegram.

Две независимые вещи:

1. HMAC-подпись callback_data мутирующих inline-кнопок. Telegram callback_data
   ограничен 64 байтами, поэтому подпись — усечённый HMAC-SHA256 (10 hex ≈ 40 бит),
   которого достаточно против подделки/устаревших кнопок из пересланных сообщений
   (в связке с deny-by-default авторизацией). Формат: ``<payload>:<tag>``.

2. ``safe_md`` — нейтрализация управляющих символов legacy-Markdown в недоверенном
   тексте (пользовательские запросы, имена товаров WB). В legacy-Markdown Telegram
   обратный слэш НЕ экранирует, поэтому единственный надёжный путь — убрать символы
   ``_ * ` [ ]``, иначе одиночный ``_`` ломает парсинг и сообщение не отправляется.
"""
from __future__ import annotations

import hashlib
import hmac
import re

_TAG_LEN = 10  # hex-символов от HMAC-SHA256


def sign_payload(payload: str, secret: str) -> str:
    """Вернуть ``payload:tag``. При пустом секрете — payload без изменений.

    Пустой секрет допустим ТОЛЬКО в shadow-режиме (см. load_config): тогда
    мутирующие кнопки всё равно защищены deny-by-default авторизацией.
    """
    if not secret:
        return payload
    tag = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:_TAG_LEN]
    return f"{payload}:{tag}"


def verify_payload(signed: str | None, secret: str) -> str | None:
    """Проверить подпись. Вернуть payload без тега, либо None при невалидной.

    При пустом секрете подпись отключена → принимаем строку как есть
    (этот путь достижим только в shadow-режиме).
    """
    if signed is None:
        return None
    if not secret:
        return signed
    payload, sep, tag = signed.rpartition(":")
    if not sep:
        return None
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:_TAG_LEN]
    if hmac.compare_digest(tag, expected):
        return payload
    return None


_MD_CONTROL = re.compile(r"[_*`\[\]]")


def safe_md(text: object) -> str:
    """Убрать управляющие символы legacy-Markdown из недоверенного текста."""
    return _MD_CONTROL.sub("", str(text))
