# Day 0 — Deployment Guide

Sprint 2026-05-15: личная арбитражная максимизация бота.

## Что сделано в коде (5 файлов, 82 строки)

- `app/scheduler.py:198` — retention теперь через config (was hardcoded 14)
- `app/config.py` — добавлены `price_history_retention_days` (default 120) и `callback_signing_secret`
- `.env.example` — новые env vars документированы
- `app/storage/migrations/` — новая директория с versioned migration system
- `app/storage/migrations/__init__.py` — registry
- `app/storage/migrations/m001_init_versioning.py` — first migration (создаёт schema_migrations table)
- `app/storage/db.py` — новый метод `apply_migrations()` после существующего `migrate()`
- `main.py` — вызывает `apply_migrations()` после `migrate()`

Все 23 существующих pytest зелёные. Smoke-test migration runner проверен: первый запуск применяет v1, второй — no-op (idempotent).

## Deploy на production VPS

```bash
# === 1. На VPS, остановить бот и сделать backup ===
ssh user@vps
cd /path/to/wb-bot
docker-compose stop
cp data/app.db data/app.db.bak-day0-$(date +%Y%m%d)
ls -la data/app.db*  # verify backup

# === 2. Подтянуть код (одним из способов) ===
# Вариант A: если репо на VPS клонирован с GitHub
git pull origin main
# (твой код только что обновлён в локальной копии; сначала push на GitHub)

# Вариант B: rsync из локалки (минуя GitHub)
# Локально:
#   rsync -avz --exclude=data --exclude=.env --exclude=.git \
#         /Users/refusned/wb-bot-sprint/ user@vps:/path/to/wb-bot/

# === 3. Обновить .env ===
nano .env  # или редактируй любым удобным способом
# Добавь:
#   PRICE_HISTORY_RETENTION_DAYS=120
#   CALLBACK_SIGNING_SECRET=e8e37193e291e7631ff4b3de31640d7663cdb14113091edbffc16a0e5df04d27
#   SHADOW_MODE=true
#
# Проверь:
#   ALLOWED_USER_IDS=<твой_telegram_id>  # должен быть НЕпустой!

# === 4. Запустить ===
docker-compose up -d --build
docker-compose logs -f --tail=50 wb_station_bot
# Ожидаемое в логах: "Applied schema migrations: [1]"
# Если "Applied schema migrations: []" — миграция уже применялась (idempotent OK)

# === 5. Проверка в Telegram ===
# /status — должно ответить нормально
# /help — список команд видим
```

## Создан backup, можно откатить:

```bash
# Если что-то пошло не так:
docker-compose stop
cp data/app.db.bak-day0-<DATE> data/app.db
# Откати код через git revert или git reset --hard <предыдущий_commit>
docker-compose up -d
```

## Что НЕ менялось

- Существующие 14 таблиц БД — нетронуты
- Существующие 25 команд бота — работают как раньше
- Inline-кнопки и callback handlers — пока не добавлены (Days 4-12 in chosen direction)
- Shadow mode пока no-op флаг (используется с Day 19)

## После deploy — Day 0 ручные действия (1 минута)

В Telegram:
1. Отправь `/status` — должно вернуть OK
2. Открой WB-кабинет → Настройки → найди свой текущий СПП в основной категории (Станция Миди)
3. Запиши значение где-то — Day 1 будет команда `/setspp_log <%>` для логирования (пока не реализована)

Готово. Day 0 закрыт. Переходим к Day 1 — lot ledger design via Codex.
