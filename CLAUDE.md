# CLAUDE.md — WB Price BOT

Контекст и принципы работы проекта для будущих сессий Claude Code.

---

## 1. О владельце и бизнес-модели

**Кто:** Refusned (GitHub), Telegram-владелец бота.

**Чем зарабатывает:** WB-to-WB арбитраж с использованием **личной СПП-скидки**.

### Схема работы

1. **Покупка:** заказываю товар на Wildberries как обычный покупатель, получая свою **личную СПП** (персональная скидка покупателя WB — у меня выше базовой из-за активной истории покупок).
2. **Продажа:** продаю тот же товар на WB как продавец (FBS режим — товар лежит у меня на руках, отгружаю в WB-склад/курьеру при заказе).
3. **Маржа** = `цена_продажи − (цена_покупки_с_личной_СПП + комиссия_WB + логистика + эквайринг + налог + holding_cost)`.

### Текущие параметры бизнеса

| Параметр | Значение |
|---|---|
| Чистая прибыль | 100–300 тыс. ₽/мес |
| Bottleneck | **Capital-bound** (сделок больше чем оборотных средств) |
| Основной товар | Яндекс Станция Миди — **ТОЛЬКО чёрная и серая** |
| Время на бота | 1–3 ч/день (хочу так же или меньше) |
| Режим работы на WB | FBS (товар у меня → отгружаю по заказу) |
| Главный риск | СПП-регуляция WB 2026 (могут срезать → схема перестанет работать) |

### Что НЕ делаю / НЕ хочу

- Не торгую другими цветами Станции (жёлтый/оранжевый/синий/малиновый/зелёный — НЕ беру)
- Не делаю мультиаккаунт СПП (юр-серая зона, риск бана)
- Не пишу SaaS / не продаю бот другим селлерам
- Не делаю one-click автопокупку (риск бана на WB)
- Не расширяюсь в Avito/Ozon (отдельный проект)
- Не делаю web-дашборд — Telegram достаточен

---

## 2. Архитектура бота

**Repo:** https://github.com/Refusned/wb-price-tracker-bot
**Локально:** `/Users/refusned/WB Price BOT/`
**Production:** VPS `ai-jobs-vps` (2.26.110.148), systemd unit `wb-bot.service`, путь `/root/WB/`.

### Стек

- Python 3.14.4
- aiogram 3.7 (Telegram)
- aiosqlite (SQLite WAL mode)
- pytest-asyncio
- aiohttp + certifi
- Codex (gpt-5.5) через `codex exec` — для генерации кода
- Развёртывание: rsync через SSH, systemd, сохраняем `data/.env/.venv` при деплое

### Основные модули

```
app/
├── bot.py                          # Dispatcher builder, регистрация handlers
├── config.py                       # AppConfig dataclass + load_config()
├── scheduler.py                    # WbUpdateScheduler — фоновые job-ы
├── logging_setup.py
├── handlers/                       # /top10, /buy, /calc, /finance, ...
│   ├── top10.py                    # фильтр по цвету (чёрн/сер whitelist)
│   ├── business.py                 # /buy + decision-purchase linking
│   ├── purchase_prompts.py         # FSM auto-prompt при приходе на FBS
│   └── ...
├── services/
│   ├── margin_calculator.py        # расчёт маржи с СПП/комиссиями
│   ├── insight_engine.py           # shadow-ban / anomaly детект
│   ├── personal_spp_auto_collector.py  # daily AVG спп из own_sales
│   ├── stock_arrival_detector.py   # детект прихода на FBS → prompt
│   └── ...
├── storage/
│   ├── db.py                       # Database, миграции m001..m007
│   ├── repositories.py             # ItemRepository, MetaRepository, ...
│   ├── business_repository.py      # purchases, own_sales, finance_journal
│   ├── decision_snapshot_repository.py  # с link_to_purchase()
│   ├── personal_spp_repository.py
│   ├── missed_deal_repository.py
│   ├── stock_arrival_repository.py
│   └── migrations/
└── wb/
    ├── client.py                   # WildberriesClient (поиск/каталог)
    ├── seller_client.py            # Seller API (FBS)
    └── parser.py
tools/
├── build_lot_ledger.py             # FIFO + phantom_opening, VACUUM INTO backup
├── analyze_baseline.py             # diagnostic report
└── rollback.sh                     # WAL-safe (.db/.db-wal/.db-shm trio)
```

### Что бот умеет (25+ команд)

- `/top10` — топ-10 цен на Станцию Миди (ТОЛЬКО чёрн/сер, whitelist)
- `/calc` — margin calculator (СПП, комиссии, логистика, эквайринг, налог)
- `/buy` — лог закупки + auto-link к decision_snapshot
- `/finance` — отчёт по выручке/комиссиям из finance_journal
- `/spp_trend` — динамика моей СПП
- Auto-detect прихода товара на FBS склад → FSM "За сколько купил?"
- Алерты на падение цены (≥5% drop, throttle)
- Daily briefing 09:00
- FIFO lot ledger с phantom_opening для legacy продаж

---

## 3. Текущее состояние (по состоянию на Day 17 / 2026-05-18)

### Завершено

- **Day 0 hotfix:** retention price_history 14 → 120 дней, schema_migrations таблица, обязательный `CALLBACK_SIGNING_SECRET`
- **Days 1-5 Foundation:**
  - Lot ledger (FIFO) — покрывает 585/776 sales = **75.4%**
  - 12 purchases с null `nm_id` смаплены через supplier_article ('019'→193961961, '020'→260407160, '22'→876392996)
  - decision_snapshots начали накапливаться с Day 0
  - personal_spp_snapshots: ручной ввод + auto AVG из own_sales 24h
- **Day 12 crash recovery:** scheduler.py восстановлен из git после Codex regen-катастрофы (141 строк вместо 500)
- **Day 16:** auto-finance sync (24h cadence), finance_journal перестал отставать на 11+ дней
- **Day 17 hotfix 3:** /top10 — strict whitelist по цветам (только чёрн/сер). Default `TOP10_INCLUDE_KEYWORDS`:
  ```
  чёрн,черн,серая,серый,серое,серого,серому,серым,black,grey,gray
  ```

### Ожидает (Day 6 GATE)

**Дата:** ~2026-05-30 (через ~2 недели от текущей даты, когда накопится данных).

**Что решить:** какое из трёх направлений делать в Days 8-19:

| Направление | Когда выбирать |
|---|---|
| **A: Capital Optimization** | если ≥40% missed deals помечены "cash-rejected" |
| **B: Speed Pipeline** | если ≥40% помечены "too_slow" (alert latency 10мин → <60сек) |
| **C: EV Calibration** | если ≥40% помечены "bad_margin" — replace heuristic risk_score через `EV = P(sell)×margin − P(return)×loss − holding_cost×E[days]` |
| **Default (если размыто)** | **C (EV Calibration)** — infrastructure-light, lowest-risk |

Playbook: `REMINDER_DAY6_GATE.md`.

### Deferred (Phase 2)

- HMAC sign/verify inline-button callbacks (env уже добавлен, реализация — нет)
- Full `/buy` nm_id resolution (если supplier_article неизвестен)
- Migration race protection
- Capital allocator (`/allocate`, `/exit_recommendations`) — после A
- Hold-time prediction regression — после A или D
- Auto-blacklist категорий с убытками

### Out of scope (никогда)

- Account profile / multi-tenant scoping
- Multi-account СПП
- Web dashboard
- Other marketplaces (Ozon/Avito)
- SaaS / billing / multi-tenant
- One-click автопокупка

---

## 4. Принципы работы (как мне писать код)

### От пользователя

- **Капитал-bound, не deal-bound:** primary metric = `profit / (invested_ruble × hold_days)`. Сделка 25% за 5 дней лучше 40% за 30.
- **Honest measurement > fake precision:** не делать counterfactual с look-ahead bias. Train 0-60, test 60-90.
- **Infrastructure before optimization:** lot ledger / decision_snapshots / retention — фундамент. Без него любая фича — гадание.
- **Shadow mode перед production:** бот считает рекомендации, я сравниваю со своими решениями 30+ дней, только потом ship full.

### Из ~/.claude/CLAUDE.md (общие правила)

1. **Think before coding** — surface assumptions, push back on overcomplicated requests
2. **Simplicity first** — минимум кода, никаких speculation/abstractions
3. **Surgical changes** — трогать только запрошенное, не "improve" соседнее
4. **Goal-driven** — verifiable success criteria, loop until verified

### Специфика этого проекта

- **Файлы >200 строк:** Codex для них генерируется ТОЛЬКО как diff/patch, никогда full rewrite (был crash Day 12)
- **SQLite WAL:** rollback — `.db + .db-wal + .db-shm` trio вместе, через systemd stop
- **Cyrillic + SQLite:** `LOWER()` в SQL не lowercase кириллицу. Фильтры — Python `str.lower()` после fetch широкого окна
- **Schema migrations:** только versioned m00X через `app/storage/migrations/`, регистрируем в `schema_migrations` таблице
- **Деплой:** `rsync --exclude data/ --exclude .env --exclude .venv` чтобы не затереть прод-данные
- **Тестирование:** pytest-asyncio, моки aiohttp через `aioresponses`, БД-фикстура `tmp_path / "test.db"`

### Чего НЕ делать

- Не предлагать SaaS-фичи (multi-tenant, billing, "продай другим селлерам")
- Не предлагать web-дашборд
- Не предлагать другие маркетплейсы (Ozon/Avito)
- Не "улучшать" соседний код при правке (P3: Surgical Changes)
- Не делать heuristic 0-100 scores — это fake precision, использовать EV или явные probabilities
- Не делать Codex full-file rewrite для файлов >200 строк

---

## 5. Production runbook

### SSH

```bash
ssh ai-jobs-vps          # → 2.26.110.148, через kent-overly jumphost
cd /root/WB
systemctl status wb-bot.service
journalctl -u wb-bot.service -f
```

### Deploy

```bash
# Из локали:
rsync -avz --exclude='data/' --exclude='.env' --exclude='.venv' --exclude='__pycache__/' \
  /Users/refusned/WB\ Price\ BOT/ ai-jobs-vps:/root/WB/
ssh ai-jobs-vps "systemctl restart wb-bot.service && sleep 3 && systemctl status wb-bot.service --no-pager | head -15"
```

### Quick env tweak (без редеплоя)

```bash
# Пример: добавить цвет в whitelist
ssh ai-jobs-vps "sed -i 's|TOP10_INCLUDE_KEYWORDS=.*|TOP10_INCLUDE_KEYWORDS=чёрн,черн,серая,серый,серое,серого,серому,серым,синяя,синий,blue,black,grey,gray|' /root/WB/.env && systemctl restart wb-bot.service"
```

### Backup перед опасной операцией

```bash
ssh ai-jobs-vps "cd /root/WB && sqlite3 data/app.db 'VACUUM INTO \"data/app.db.bak-$(date +%Y%m%d-%H%M%S)\"'"
```

### Rollback

```bash
ssh ai-jobs-vps "cd /root/WB && bash tools/rollback.sh"
# Скрипт сам остановит service, перенесёт latest backup, перезапустит
```

---

## 6. Ключевые таблицы БД

| Таблица | Что хранит | Заметки |
|---|---|---|
| `items` | топ-100 SKU из WB-каталога по запросу | обновляется каждые 10 мин |
| `price_history` | scanned price per nm_id over time | retention 120 дней (был 14 до Day 0) |
| `purchases` | мои закупки (date, nm_id, qty, price) | `nm_id` может быть NULL (legacy) |
| `own_sales` | finalized sales из Seller Statistics API | source of truth для FIFO |
| `own_orders` | orders (включая cancelled) | НЕ source для FIFO (E12) |
| `finance_journal` | удержания/комиссии из finance API | auto-sync 24h (Day 16) |
| `lots` | derived FIFO лоты | `lot_id = "p:{purchase_id}"`, status: open/closed/phantom_opening |
| `lot_allocations` | row-level events (sale/return/adjustment) | E3 — позволяет reverse return |
| `decision_snapshots` | каждый алерт/предложение бота | linked к purchase через `purchase_id` |
| `personal_spp_snapshots` | моя СПП weekly (manual + auto) | proxy для регуляторного monitoring |
| `stock_arrivals` | детект прироста stock на FBS | trigger FSM "за сколько купил?" |
| `subscribers` | Telegram users (allowlist через ALLOWED_USER_IDS) | hard owner-check для мутаций |
| `tracked_articles` | hot-watch list nm_id | seed для Speed Pipeline (Direction B) |
| `schema_migrations` | version, applied_at | E2 — версионирование |

---

## 7. Quick reference

- Текущая дата: **2026-05-18**
- Sprint Day: **17**
- До Day 6 GATE: **~12 дней** (2026-05-30)
- Lot ledger coverage: **75.4%** (585/776 sales)
- Последний коммит: `a5222d9 Day 17 hotfix 3: strict whitelist`
- Production: ✅ active (verified 10/10 чёрн/сер в /top10)
