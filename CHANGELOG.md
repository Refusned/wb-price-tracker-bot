# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/) · версионирование [SemVer](https://semver.org/lang/ru/).

## [1.0.0] — 2026-05-21

Первый тегированный релиз. Бот стабильно работает 24/7 в Docker на VPS.

### Возможности

- **Мониторинг рынка**: фоновый поллинг каталога Wildberries с настраиваемым
  интервалом (`WB_POLL_INTERVAL_SECONDS`, по умолчанию 600 сек), поиск по
  нескольким вариантам запроса с дедупликацией, retry/backoff к WB API
- **Алерты**: push-уведомления при падении цены, настраиваемый порог и cooldown
- **Аналитика продавца через WB Seller API**: сводки по заказам, выкупам,
  остаткам, возвратам; отдельный интервал поллинга Seller API
- **Финансовый учёт**: маржинальный калькулятор с учётом налога, эквайринга,
  логистики, СПП; учёт закупок и себестоимости; отчёты по прибыли и cashflow
- **Команды**: `/briefing`, `/top10`, `/calc`, `/finance`, `/profit`, `/costs`,
  `/deals`, `/purchases`, `/reorder`, `/returns`, `/spp`, `/cashflow` и другие
- **Хранилище**: SQLite (aiosqlite), repository-паттерн, переживает рестарт

### Инфраструктура

- aiogram v3, aiohttp, asyncio
- Docker + docker-compose
- GitHub Actions CI: pytest на Python 3.11 и 3.12
- 23 unit-теста: парсер WB API, маржинальный калькулятор, алерты, фильтрация

### Известные ограничения

- Single-instance: один процесс поллинга, FSM-состояния в памяти
- SQLite single-writer (для текущей нагрузки не узкое место)
- WB API публичный — структура ответов может меняться без анонса

[1.0.0]: https://github.com/Refusned/wb-price-tracker-bot/releases/tag/v1.0.0
