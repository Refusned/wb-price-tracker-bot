# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) · версионирование [SemVer](https://semver.org/lang/ru/).

## [Unreleased]

### Security
- Deny-by-default авторизация: пустой `ALLOWED_USER_IDS` теперь закрывает
  доступ всем (раньше — открывал). Глобальный access-middleware.
- HMAC-подпись мутирующих inline-кнопок (`md:*`, `purprompt:*`) +
  startup-check на `CALLBACK_SIGNING_SECRET` при `SHADOW_MODE=false`.
- `SHADOW_MODE` теперь реально применяется (отключает автономные наблюдения).
- Удалены утёкшие операционные заметки из репозитория и его истории
  (инфраструктура, секрет); добавлены в `.gitignore`/`.dockerignore`.
- Docker: non-root пользователь, HEALTHCHECK, `.env`/`data/` исключены из образа.

### Fixed
- Денежная безопасность: гейт тарифов AND→OR (алерт не уходит с
  захардкоженным тарифом); атомарный `add_purchase` (без гонки rowid);
  таймзона брифинга (МСК) + персист даты; Seller API бросает ошибку вместо
  тихого `[]`.
- Ретеншен `arb_candidates`/наблюдений (БД больше не растёт бесконечно).
- Markdown-экранирование пользовательского текста и имён товаров.
- Трейсбеки больше не утекают пользователю; лимит длины `/spp_history`.
- Реализована команда `/insights` (была в меню без обработчика).

### Added
- Тесты денежной формулы (`compute_arbitrage_margin` по ground-truth),
  атомарности вставок, ретеншена и гейта тарифов. Всего 112 тестов.
- CI: lint (ruff) + type-check (mypy, advisory) + coverage gate.
- `requirements-dev.txt`, `ruff.toml`, `mypy.ini`, `healthcheck.py`.

## [1.0.0] — 2026-05-21

Первый тегированный релиз. Бот стабильно работает 24/7 в Docker на VPS.

### Возможности

- **Мониторинг рынка**: фоновый поллинг каталога Wildberries с настраиваемым
  интервалом (`WB_POLL_INTERVAL_SECONDS`), поиск по нескольким вариантам запроса
  с дедупликацией, retry/backoff к WB API
- **Алерты цен**: push-уведомления при падении цены в отслеживаемой выборке,
  настраиваемый порог и cooldown между алертами
- **Аналитика продавца через WB Seller API**: сводки по заказам, выкупам,
  остаткам, возвратам; отдельный интервал поллинга Seller API
- **Финансовый учёт**: маржинальный калькулятор с учётом налога, эквайринга,
  логистики и СПП; учёт закупок и себестоимости (FIFO lot ledger);
  отчёты по прибыли, costs и cashflow; ABC-анализ
- **Команды**: `/briefing`, `/top10`, `/calc`, `/finance`, `/profit`, `/costs`,
  `/deals`, `/purchases`, `/reorder`, `/returns`, `/spp`, `/cashflow`, `/buy`,
  `/month` и набор `set*`-команд для параметров расчёта
- **Хранилище**: SQLite (aiosqlite), repository-паттерн, переживает рестарт;
  детектор поступлений товара, авто-сбор персонального СПП

### Инфраструктура

- aiogram v3, aiohttp, asyncio
- Docker + docker-compose
- GitHub Actions CI: pytest на Python 3.11 и 3.12
- 76 unit-тестов: парсер WB API, маржинальный калькулятор, алерты, фильтрация,
  репозитории решений, FIFO-учёт партий, детектор поступлений, сбор СПП

### Известные ограничения

- Single-instance: один процесс поллинга, FSM-состояния в памяти
- SQLite single-writer (для текущей нагрузки не узкое место)
- Публичный WB API: структура ответов меняется без анонса (за разработку
  была миграция на эндпоинты v18) — парсеры покрыты тестами с фикстурами

[1.0.0]: https://github.com/Refusned/wb-price-tracker-bot/releases/tag/v1.0.0
