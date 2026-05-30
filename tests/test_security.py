"""Тесты HMAC-подписи callback'ов, safe_md и deny-by-default авторизации."""
from __future__ import annotations

import dataclasses

import pytest

from app.config import AppConfig, load_config
from app.security import safe_md, sign_payload, verify_payload

SECRET = "0123456789abcdef0123456789abcdef"


def test_sign_verify_roundtrip() -> None:
    payload = "md:cash:304333036:2026-05-29"
    signed = sign_payload(payload, SECRET)
    assert signed != payload
    assert verify_payload(signed, SECRET) == payload


def test_verify_rejects_tampered_tag() -> None:
    signed = sign_payload("purprompt:price:42", SECRET)
    tampered = signed[:-1] + ("0" if signed[-1] != "0" else "1")
    assert verify_payload(tampered, SECRET) is None


def test_verify_rejects_unsigned_when_secret_set() -> None:
    # Подделка без тега должна быть отвергнута.
    assert verify_payload("md:cash:1:2026-01-01", SECRET) is None


def test_verify_rejects_none() -> None:
    assert verify_payload(None, SECRET) is None


def test_empty_secret_is_passthrough() -> None:
    # Shadow-режим: подпись отключена, payload проходит как есть.
    payload = "md:skip"
    assert sign_payload(payload, "") == payload
    assert verify_payload(payload, "") == payload


def test_signed_callback_fits_telegram_64_byte_limit() -> None:
    # Самый длинный реальный payload + тег должен влезать в 64 байта.
    payload = "md:not_interested:999999999:2026-12-31"
    signed = sign_payload(payload, SECRET)
    assert len(signed.encode()) <= 64


def test_safe_md_strips_markdown_controls() -> None:
    assert safe_md("Колонка _Миди_ *NEW* `code` [x]") == "Колонка Миди NEW code x"


def test_safe_md_accepts_non_str() -> None:
    assert safe_md(123) == "123"


# ── deny-by-default авторизация ──────────────────────────────────────


def _cfg(monkeypatch, **overrides) -> AppConfig:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    cfg = load_config()
    return dataclasses.replace(cfg, **overrides)


def test_empty_whitelist_denies_everyone(monkeypatch) -> None:
    cfg = _cfg(monkeypatch, allowed_user_ids=set())
    assert cfg.is_user_allowed(123) is False
    assert cfg.is_user_allowed(None) is False


def test_whitelist_allows_only_listed(monkeypatch) -> None:
    cfg = _cfg(monkeypatch, allowed_user_ids={42})
    assert cfg.is_user_allowed(42) is True
    assert cfg.is_user_allowed(7) is False


def test_secret_required_when_not_shadow(monkeypatch) -> None:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("SHADOW_MODE", "false")
    monkeypatch.setenv("CALLBACK_SIGNING_SECRET", "")
    with pytest.raises(ValueError, match="CALLBACK_SIGNING_SECRET"):
        load_config()


def test_shadow_mode_allows_empty_secret(monkeypatch) -> None:
    monkeypatch.setattr("app.config.load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOT_TOKEN", "test-token")
    monkeypatch.setenv("SHADOW_MODE", "true")
    monkeypatch.setenv("CALLBACK_SIGNING_SECRET", "")
    cfg = load_config()
    assert cfg.shadow_mode is True
