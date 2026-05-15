# Sprint Status — wb-price-tracker-bot

Sprint 2026-05-15: личная арбитражная максимизация бота.

## Состояние: Days 0-1-5 + Day 7 quick wins committed

| Day | Commit | Что вошло |
|---|---|---|
| 0 | `42893ab` | Schema migration system, retention 14→120 days, callback secret env |
| 1 | `8cd97b7` | Lot ledger (FIFO+events), decision_snapshots, personal_spp_snapshots |
| 5 | `2c78b88` | Diagnostic baseline + /setspp_log + /missed_deals FSM |
| 7q | (uncommitted) | tools/rollback.sh — atomic rollback к backup |

**Метрики:**
- 40/40 pytest tests pass (23 existing + 17 new)
- 23 files changed cumulative, +2280 lines, -2 lines
- 3 smoke tests done end-to-end:
  - Migration runner idempotent (1→4 → no-op)
  - Lot ledger backfill: 7 allocations, 2 lots, return correctly attributed, idempotent rerun
  - Baseline report: full markdown output, all 6 sections

## Что бот теперь умеет (новое)

| Команда | Описание |
|---|---|
| `/setspp_log <%>` | Логирование текущей личной СПП. Записывает в `personal_spp_snapshots`. |
| `/spp_history [days]` | Последние N дней СПП-снимков (default 30). |
| `/spp_trend` | 7-дневный тренд: current, mean, min, max, drop_pct. ⚠️ alert при drop ≥ 15%. |
| `/missed_deals` | FSM-диалог: показывает 10-15 candidates с inline-кнопками (cash/too_slow/bad_margin/not_interested/skip/stop). Записывает в `missed_deal_tags`. |

## Что бот теперь умеет (инструменты под капотом)

| Инструмент | Что делает |
|---|---|
| `python3 tools/build_lot_ledger.py` | Backup-first, idempotent. Привязывает каждую `own_sales` к `lots` через FIFO. Возвраты — к тому же лоту что и original sale (FIFO oldest un-returned). |
| `python3 tools/analyze_baseline.py` | Markdown-отчёт в `data/reports/baseline-YYYYMMDD.md`. Lot coverage, profit_per_ruble_day per category, hold-time, latency, СПП history, missed-deal candidate count. |
| `bash tools/rollback.sh [path]` | Откат к latest backup (или специфическому). Безопасно: текущий DB не удаляется, переименовывается. |

## Deploy на VPS (сейчас!)

```bash
# === 1. На VPS, стоп + backup ===
ssh user@vps
cd /path/to/wb-bot
docker-compose stop
mkdir -p data/backups
cp data/app.db data/app.db.bak-day0-$(date +%Y%m%d)

# === 2. Обновить код ===
# Вариант A (если репо на VPS клонирован с GitHub):
#   git pull origin main   # после твоего push с локалки
# Вариант B (rsync):
#   На локалке:
#   rsync -avz --exclude=data --exclude=.env --exclude=.git --exclude=__pycache__ \
#         /Users/refusned/wb-bot-sprint/ user@vps:/path/to/wb-bot/

# === 3. Обновить .env (на VPS) ===
nano .env
# Добавь:
#   PRICE_HISTORY_RETENTION_DAYS=120
#   CALLBACK_SIGNING_SECRET=e8e37193e291e7631ff4b3de31640d7663cdb14113091edbffc16a0e5df04d27
#   SHADOW_MODE=true
# Проверь:
#   ALLOWED_USER_IDS=<твой_telegram_id>   # обязательно непустой

# === 4. Старт + backfill ===
docker-compose up -d --build
docker-compose logs --tail=50  # ожидать "Applied schema migrations: [1, 2, 3, 4, 5]"

# Один раз — backfill lot ledger (внутри контейнера):
docker-compose exec wb_station_bot python3 tools/build_lot_ledger.py
# Ожидать "Lot ledger build complete: ..." и список созданных лотов

# === 5. Verify в Telegram ===
# /status — должно ответить
# /setspp_log 24    — попробуй залогить (поменяй на свою актуальную СПП)
# /spp_trend        — должен показать что-то (всего 1 запись пока)
# /missed_deals     — может ничего не показать, если price_history пустая
```

## Daily rituals (после deploy)

1. **Каждое утро (10 секунд):** Открой WB-кабинет, посмотри свою личную СПП. В Telegram: `/setspp_log <процент>`.
2. **Раз в 3-5 дней:** `/missed_deals` — если бот накопил кандидатов, тегни 10-15.
3. **Раз в неделю:** `python3 tools/analyze_baseline.py` внутри контейнера, прочитай отчёт.

## Что осталось в спринте

| Day | Что | Статус |
|---|---|---|
| 2-4 | Lot ledger + decision_snapshots + СПП daily backfill | ✅ Done (Day 1 commit) |
| 5 | Diagnostic baseline + manual tagging | ✅ Done |
| 6 | **GATE — выбор ОДНОГО направления** | ⏳ **Manual decision** — после 1-2 недель data |
| 7 | Production safety setup | 🟡 Partial (rollback есть, нужна доработка staging clone) |
| 8-19 | Implementation chosen direction | ⏳ После Day 6 |
| 17 | СПП Regime Monitor | ⏳ Финал |
| 22 | Counterfactual measurement | ⏳ Day 22 = "instrumentation ready", validation 30-60 days после |

## Day 6 GATE — как принять решение

После 1-2 недель работы (накопить данные):

1. `python3 tools/analyze_baseline.py` — открой отчёт
2. `/missed_deals` — протегай 10-15 кандидатов
3. Запусти `python3` в контейнере:
   ```python
   import asyncio
   from app.storage.db import Database
   from app.storage.missed_deal_repository import MissedDealRepository

   async def main():
       db = Database('data/app.db')
       await db.connect()
       repo = MissedDealRepository(db)
       print(await repo.distribution())
       await db.close()
   asyncio.run(main())
   ```
4. Посмотри распределение:
   - **cash ≥ 40%** → Direction 1: Capital Optimization
   - **too_slow ≥ 40%** → Direction 2: Speed Pipeline
   - **bad_margin ≥ 40%** → Direction 3: EV Calibration
   - **Размыто** → Default Direction 3 (lowest-risk infrastructure)

После выбора — следующий чанк имплементации.

## Token budget note

Sprint так-far использует Codex как primary implementation partner:
- Day 1: 1 Codex call → 7 files generated (lot ledger, repository, backfill, tests)
- Day 5: 1 Codex call → 11 files generated (baseline, repos, handlers, FSM, tests)
- 2 Codex calls total = ~110k Codex tokens spent
- Claude tokens are mostly orchestration: parsing output, smoke testing, writing prompts, integrating

Каждый следующий чанк ~1 Codex call за раз. Day 6 — no code (manual decision). Days 8-19 — depends on chosen direction (1-3 Codex calls).
