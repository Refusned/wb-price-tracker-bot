"""Регрессия на класс бага «голый _ в legacy-Markdown валит сообщение».

Telegram parse_mode="Markdown" трактует «_» как начало курсива даже внутри
слова: непарный «_» → TelegramBadRequest «can't parse entities», сообщение
НЕ отправляется, пользователь видит тишину. Так молчали кнопка «Аналитика»
(/spp_trend в тексте) и /arb (wrong_color). Внутри `code`-спанов «_» безопасен.

Тест сканирует исходники: во всех статических литералах сообщений,
отправляемых с parse_mode="Markdown", не должно остаться голых «_»
вне бэктиков. Динамика не проверяется — она обязана идти через safe_md().
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / "app"

_SEND_CALL = re.compile(r"\.(?:answer|edit_text|reply|send_message)\s*\(")


def _markdown_calls(src: str):
    """(номер строки, текст вызова) для каждой отправки с parse_mode="Markdown"."""
    for m in _SEND_CALL.finditer(src):
        start = m.end() - 1
        depth, i = 0, start
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        call = src[start : i + 1]
        if 'parse_mode="Markdown"' in call:
            yield src[:start].count("\n") + 1, call


def _bare_underscores(call: str) -> int:
    literals = re.findall(r'"((?:[^"\\]|\\.)*)"', call)
    text = "".join(lit for lit in literals if lit != "Markdown")
    no_code = re.sub(r"`[^`]*`", "", text)       # внутри `код` безопасно
    no_dynamic = re.sub(r"\{[^}]*\}", "", no_code)  # f-подстановки → safe_md
    return no_dynamic.count("_")


def test_no_bare_underscores_in_static_markdown_texts() -> None:
    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        for line_no, call in _markdown_calls(src):
            bare = _bare_underscores(call)
            if bare:
                offenders.append(f"{path.relative_to(APP.parent)}:{line_no} — {bare} голых «_»")
    assert not offenders, (
        "Голые «_» вне `бэктиков` в parse_mode=\"Markdown\" текстах "
        "(непарный — сообщение не отправится):\n" + "\n".join(offenders)
    )
