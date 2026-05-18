# CLAUDE.md — WB Price BOT

Контекст, бизнес-модель и принципы работы для будущих сессий Claude Code.
**Последнее обновление: 2026-05-18 (Day 18, evening)**

---

## ⚡ Quick Resume (для следующей сессии)

**Что сделано сегодня (Day 18):**
- Полный арбитражный сканер задеплоен в продакшен (commit `bb73094`)
- Все WB API hotfixes применены (v18 endpoint, Brotli, price schema)
- Критичный double-СПП bug найден и пофикшен
- Math validated на ground truth арт 876392996
- **Первые real arbitrage alerts** ушли в Telegram

**Что прямо сейчас работает в проде:**
- Bot: `ai-jobs-vps:/root/WB/` (systemd `wb-bot.service`)
- Scanner крутится каждые 10 мин с threshold `margin≥4%, profit≥300₽, PPRD≥0.5%`
- 2 active queries в БД: "Станция Миди", "робот пылесос xiaomi"
- 5 observations записано (все для категории 8899 - Умная колонка)
- Категория 2791 (Роботы-пылесосы) — пока без observations → 0 candidates

**Завтра делать (NEXT STEPS):** см. секцию §8 ниже.

---

## 1. Кто я и бизнес-модель

**Кто:** Refusned (GitHub), Telegram-владелец бота. WB-to-WB арбитражер.

**Чем зарабатывает:** структурный спред между ролями buyer и seller на одной площадке WB.

### Текущие параметры

| Параметр | Значение |
|---|---|
| Чистая прибыль | 100–300 тыс. ₽/мес |
| Bottleneck | **Capital-bound** (сделок больше чем оборотных) |
| Основной товар | Яндекс Станция Миди — **только чёрная и серая** |
| Цена/штука | ~15 000₽ listed, ~12 000₽ revenue, ~10 500₽ моя buy |
| Маржа/штука | ~500–1 200₽ (3-8% в зависимости от nm) |
| Объём | ~200 штук/мес (verified math) |
| Налог | УСН 2% (региональная льгота) |
| Время на бота | 1–3 ч/день |
| Главный риск | WB-Скидка категорий — плавает 21-25%/мес, может срезаться |

### Что НЕ делаю / НЕ хочу

- Не торгую другими цветами Станции (жёлт/оранж/синий/малиновый — нет)
- Не делаю мультиаккаунт СПП (юр-серая зона)
- Не пишу SaaS / не продаю бот другим селлерам
- Не делаю one-click автопокупку (риск бана на WB)
- Не расширяюсь в Avito/Ozon
- Не делаю web-дашборд — Telegram достаточен

---

## 2. 💰 МОДЕЛЬ ЦЕНООБРАЗОВАНИЯ WB 2026 (КРИТИЧНО!)

**Это центральная модель. Все расчёты scanner на ней. Не меняй без verified ground truth.**

### Слои цены (verified владельцем на арт. 876392996)

```
═══════════════════════════════════════════════════════════════════════
СЛОЙ                              ЗНАЧЕНИЕ    КТО КОНТРОЛИРУЕТ / ПЛАТИТ
═══════════════════════════════════════════════════════════════════════
RRC (рекомендованная розничная)   29 999₽    селлер выставляет
   ↓ seller_discount = 50%                   селлер (моя политика)
listed_price                      15 000₽    ← БАЗА ВЫРУЧКИ СЕЛЛЕРА
   ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓ ↓
   ↓
   ↓ ВЫПЛАТА СЕЛЛЕРУ (на расч. счёт):
   ↓ ─ commission FBS (~16%)      −2 400₽
   ↓ ─ logistics (FBS короб)        −500₽
   ↓ ─ acquiring / эквайринг        −150₽
   ↓ ─ return reserve (3% × 0.5)    −225₽
   ↓ ─ tax УСН 2%                   −234₽
   ↓ = ppvz_for_pay              11 000-12 000₽   ← селлер РЕАЛЬНО получает
   ↓
   ↓ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
   ↓
   ↓ WB-Скидка (бывш. СПП) категории, плавает 21-25% помесячно
   ↓ ═══ КРИТИЧНО: ФИНАНСИРУЕТ WB ЗА СВОЙ СЧЁТ ═══
   ↓ Селлерная выручка НЕ уменьшается (оферта WB п. 5.4)
   ↓ Категория-широкая: одинакова для ВСЕХ buyers в категории.
   ↓   - Умные колонки: 21-25%
   ↓   - Роботы-пылесосы: 30%
   ↓
   ↓ Например для колонок: 15 000 × (1 - 0.25) = 11 250₽
   ↓ ↓ Это поле sizes[0].price.product в u-search.wb.ru/v18 ← scanner парсит это
   ↓
buyer_price_after_spp             11 250₽    видит обычный покупатель
   ↓
   ↓ WB-Кошелёк bonus (6%) — личный, для оплаты с WB-Кошелька
   ↓ Реальный checkout-дисконт, НЕ cashback
   ↓ 11 250 × (1 - 0.06) = 10 575₽
   ↓
buyer_pays_at_checkout            10 658₽    ← владелец РЕАЛЬНО платит как buyer
═══════════════════════════════════════════════════════════════════════
```

### Откуда деньги (структурный спред между ролями)

```
КАК ПОКУПАТЕЛЬ у чужого селлера:
  плачу = chuzhoi_listed × (1 - cat_СПП) × (1 - wallet)
  для колонок = chuzhoi_listed × 0.75 × 0.94 = chuzhoi_listed × 0.705

КАК СЕЛЛЕР перепродаю тот же товар:
  получаю = my_listed × 0.78  (после всех WB-удержаний)

ПРИБЫЛЬ при my_listed ≈ chuzhoi_listed:
  margin = listed × (0.78 - 0.705) = listed × 7.5%
  для категории с СПП 30%: margin = listed × 12.2%
```

**На 15 000₽ × 7.5% × 200 шт/мес ≈ 225к/мес** — соответствует диапазону 100-300к/мес.

### КЛЮЧЕВЫЕ выводы для кода

1. **WB API `sizes[0].price.product` = buyer_price_after_СПП** (НЕ listed_price!). Это price которую видит обычный buyer на сайте, не моя розничная.

2. **СПП — категория-широкая константа**, одинакова для всех buyers. Не персональная.

3. **WB-Кошелёк 6%** — единственный персональный edge (для buyers WB-Кошелёк).

4. **Selling revenue base = my_listed_price**, не buyer_price. WB финансирует СПП.

5. **Observed composite SPP = `1 - (paid/public)` = cat_СПП + wallet baked together**. Чтобы получить cat_СПП из composite: `cat = 1 - (1-composite)/(1-wallet)`. Например 28.95% composite + 6% wallet → cat=24.4%.

### Где БЫЛ bug (Day 18 evening)

Раньше `compute_arbitrage_margin` делал `buy_price = market × (1 - composite_SPP)` — это применяло СПП дважды (один раз WB API, второй раз scanner). Реальная buy_price была занижена на ~2000₽ → margin отрицательная → 0 alerts.

**Правильно:** `buy_price = market × (1 - wallet_only)`. Listed_implied вычисляется обратно: `listed = market / (1 - cat_СПП)`. Revenue = listed × 0.78.

---

## 3. Архитектура бота

**Repo:** https://github.com/Refusned/wb-price-tracker-bot
**Локально:** `/Users/refusned/Desktop/WB Price BOT/`
**Production:** VPS `ai-jobs-vps` (2.26.110.148), systemd `wb-bot.service`, `/root/WB/`
**Telegram:** @price_of_wb_bot (id 8678305855)

### Стек
- Python 3.12 (prod) / 3.14.4 (local)
- aiogram 3.7, aiosqlite, aiohttp
- Brotli (REQUIRED — WB returns brotli-encoded responses)
- pytest-asyncio (76 тестов на Day 18)

### Структура app/

```
app/
├── bot.py                            Dispatcher, регистрация routers
├── config.py                         AppConfig dataclass + load_config
├── scheduler.py                      WbUpdateScheduler (cron jobs)
├── arbitrage/                        ★ Day 18 submodule
│   ├── __init__.py
│   ├── scanner.py                    ArbitrageScanner orchestrator
│   ├── spp_resolver.py               PersonalSppResolver (nm → category_avg)
│   ├── margin.py                     compute_arbitrage_margin (ПРАВИЛЬНАЯ модель)
│   ├── tariffs_cache.py              WB tariffs/commission + tariffs/box
│   ├── tariffs_repository.py
│   ├── repository.py                 arb_queries, arb_candidates, observations
│   ├── auto_observer.py              ★ Auto-observe on /buy
│   ├── handlers.py                   /arb_* commands
│   └── formatting.py                 Telegram alert builder
├── handlers/
│   ├── main_menu.py                  ★ Reply-keyboard главное меню
│   ├── top10.py, business.py         (existing)
│   ├── purchase_prompts.py           FSM, hooked для auto-observe
│   └── ...
├── services/
│   ├── margin_calculator.py          (legacy, NOT для arbitrage)
│   ├── insight_engine.py
│   ├── personal_spp_auto_collector.py
│   └── stock_arrival_detector.py
├── storage/
│   ├── db.py, repositories.py
│   ├── business_repository.py        + list_recent_own_nm_ids
│   ├── migrations/m001..m008         ★ m008 = arbitrage schema
│   └── ...
└── wb/
    ├── client.py                     ★ Updated headers + Brotli
    ├── endpoints.py                  ★ u-search.wb.ru/v18 PRIMARY
    ├── seller_client.py
    └── parser.py
```

---

## 4. Day 18 — что сделано (10 коммитов)

| Commit | Описание |
|---|---|
| `29945dc` | Day 18 initial: scanner submodule (1990 lines) |
| `5ee1a81` | /review fixes: cohort subject filter + alert gating |
| `584d3ff` | WB API hotfix: `search.wb.ru/v14` → `u-search.wb.ru/v18` |
| `0c165a2` | WB sort=priceup → 0 products on v18, switched to sort=popular |
| `8b61f3d` | v18 flat products + Brotli requirement |
| `9504b85` | v18 prices in `sizes[0].price.product` (not `priceU`) |
| `d37afb1` | Skip cohort outliers (price < P25/2 = accessory) |
| `5347733` | main_menu router + "↩️ Главное меню" button |
| `67211be` | UX: scanner shows cohort_size + needs-obs hint |
| `addf609` | Auto-observe on /buy + /arb_quickadd + /arb_bulk |
| `bb73094` | **CRITICAL: fix double-СПП + decompose cat_spp + wallet** |

### Production state (Day 18 evening)

```
Scanner: ✅ running
Threshold: PPRD≥0.5% AND profit≥300₽ AND margin≥4%
Queries: 2 active
  #2 "Станция Миди" → subj 8899 (Умная колонка), cohort 157
  #3 "робот пылесос xiaomi" → subj 2791 (Роботы-пылесосы), cohort 98
Observations: 5 (все в subj 8899)
Tariffs cache: 7412 commissions + 89 warehouses
First real alerts sent: ✅ (nm 216880682, 304333036)
```

### Math validation (на арт. 876392996)

| | Owner reality | Scanner calc | Δ |
|---|---|---|---|
| Listed | 15 000₽ | 14 803₽ | -1.3% |
| My checkout | 10 658₽ | 10 518₽ | -140₽ |
| Revenue от WB | 11-12k₽ | 11 713₽ | ✓ |
| **Net margin** | ~1 000₽ | **891₽** | ✓ |
| At 200 шт/мес | 200-240k | **178k** | ✓ |

---

## 5. Команды бота (Day 18 final)

### Главное меню (`/start` или `/menu`)
- 🎯 Арбитраж → submenu
- 💰 Финансы → cheat-sheet /finance/abc/buy/calc
- 📊 Аналитика → /briefing/top10/insights
- ⚙️ Настройки → /help/setmin/status

### /arb_* (арбитражный сканер)

| Команда | Описание |
|---|---|
| `/arb` | Submenu (Свежие связки / Мои запросы / Моя СПП / Топ категории) |
| `/arb_add <фраза>` | Добавить поисковый запрос |
| `/arb_list` | Мои запросы с cohort size + hint про observations |
| `/arb_remove <id\|фраза>` | Отключить |
| `/arb_quickadd <nm> <моя_цена>` | Auto-fetch публичной цены, считает СПП |
| `/arb_bulk` | Массовый paste пар `nm price` |
| `/arb_observe <nm> <моя> <публич>` | Ручное наблюдение |
| `/arb_my_spp` | Таблица моей СПП по категориям |
| `/arb_top_cat` | Топ-5 категорий с высокой СПП |
| `/arb_deals` | Свежие candidates (24ч) |
| `/arb_scan_now` | Force scan |

### Авто-observe

- При `/buy <qty> <price> <nm>` бот автоматически fetches public price + records observation
- При FSM auto-prompt со stock-arrival — то же самое

---

## 6. Текущая БД (`data/app.db` на проде)

### Ключевые таблицы

| Таблица | Описание | Записей |
|---|---|---|
| `arb_queries` | Поисковые запросы scanner | 2 |
| `arb_candidates` | Кандидаты с margin breakdown | 152+ |
| `arb_buyer_spp_observations` | Observations (composite СПП) | 5 |
| `arb_tariffs_commission` | WB commission per subject | 7412 |
| `arb_tariffs_box` | FBS logistics per warehouse | 89 |
| `purchases` | Мои закупки (legacy + new) | many |
| `own_sales` | Sales из Seller API | many |
| `decision_snapshots` | Audit trail алертов | many |

### Schema migrations

m001..m008 все applied. m008 = arbitrage schema (см. `app/storage/migrations/m008_arbitrage.py`).

---

## 7. Принципы работы (для будущих сессий)

### От пользователя

- **Capital-bound, not deal-bound:** primary metric = `profit_per_ruble_day` (ROI/день), не margin %
- **Honest measurement > fake precision:** no look-ahead bias, no fake confidence
- **Infrastructure before optimization:** lot ledger, observations, tariffs — фундамент
- **Shadow mode перед production:** бот считает, я сравниваю с ручными решениями, потом ship full

### Из ~/.claude/CLAUDE.md

1. Think before coding — surface assumptions
2. Simplicity first — никаких speculative features
3. Surgical changes — только запрошенное
4. Goal-driven — verifiable success criteria

### Специфика проекта

- **WB API волатилен** — endpoints меняются (v9-v18 за год). Если scanner ломается, всегда проверь curl на u-search.wb.ru/v18.
- **Brotli REQUIRED** — без него aiohttp возвращает 0 products silently
- **Cyrillic + SQLite:** `LOWER()` не работает с кириллицей, фильтруй через Python `str.lower()`
- **Файлы >200 строк:** Codex для них генерируется как diff/patch, не full rewrite (был crash Day 12)
- **Money correctness:** ВСЕ изменения в `margin.py` валидируй на ground truth (арт 876392996, см. §2 выше)

### Чего НЕ делать

- Не предлагать SaaS, web dashboard, multi-tenant
- Не делать heuristic 0-100 scores — fake precision
- Не "улучшать" соседний код (P3)
- Не делать Codex full-rewrite для >200 строк
- Не применять СПП **дважды** в margin formula (был critical bug Day 18)

---

## 8. 🌅 Что делать завтра (NEXT STEPS)

### Приоритет 1: Bootstrap observations для роботов-пылесосов
- Открой WB → найди 3-5 роботов-пылесосов Xiaomi
- Для каждого: `/arb_quickadd <nm_id> <твоя_checkout_цена>`
- После этого scanner начнёт alert-ить cohort 98 SKU в категории 2791

### Приоритет 2: Tune threshold по data
- Текущий: `margin≥4%, profit≥300, PPRD≥0.5%`
- За день данных: посмотри `/arb_deals` — какие margin реалистичны?
- Adjust ENV без deploy: `ssh ai-jobs-vps "sed -i 's|ARBITRAGE_MIN_MARGIN_PERCENT=.*|ARBITRAGE_MIN_MARGIN_PERCENT=3.5|' /root/WB/.env && systemctl restart wb-bot.service"`

### Приоритет 3: Добавить ещё категории
- `/arb_add Кофемашина De'Longhi`
- `/arb_add Naturecan протеин`
- Etc. — те где у тебя СПП ≥25%

### Phase 2 (later):
- Cookie path: реализовать PoW solver + auto-fetch personal price (3-4h работы)
- Hold-time prediction через recent sales velocity (Codex #4 deferred)
- Weighted SPP avg by sample_count (Codex #3 deferred)
- Calibration loop через finance_journal (Day 30+)
- Inline-buttons "✅ Купил" / "🚫 Игнор" на alerts с HMAC

### Day 6 GATE (на паузе)
- Был запланирован на ~2026-05-30
- Sprint про arbitrage scanner его subsumes — Direction A/B/C из плана можно реактивировать после shadow-mode periоd

---

## 9. Production runbook

### SSH к серверу
```bash
ssh ai-jobs-vps                       # 2.26.110.148 через kent-overly jumphost
cd /root/WB
systemctl status wb-bot.service
journalctl -u wb-bot.service -f
```

### Quick env tweak (без редеплоя)
```bash
ssh ai-jobs-vps "sed -i 's|^ARBITRAGE_MIN_MARGIN_PERCENT=.*|ARBITRAGE_MIN_MARGIN_PERCENT=3.5|' /root/WB/.env && systemctl restart wb-bot.service"
```

### Backup перед опасной операцией
```bash
ssh ai-jobs-vps "cd /root/WB && sqlite3 data/app.db 'VACUUM INTO \"data/app.db.bak-$(date +%Y%m%d-%H%M%S)\"'"
```

### Deploy (полный rsync)
```bash
cd "/Users/refusned/Desktop/WB Price BOT"
rsync -avz --exclude='data/' --exclude='.env' --exclude='.venv' --exclude='__pycache__/' \
  --exclude='.git' --exclude='.pytest_cache' \
  ./ ai-jobs-vps:/root/WB/
ssh ai-jobs-vps "systemctl restart wb-bot.service && sleep 5 && journalctl -u wb-bot.service -S '15 seconds ago' --no-pager | head -10"
```

### Deploy одного файла
```bash
rsync -avz "/Users/refusned/Desktop/WB Price BOT/app/arbitrage/scanner.py" \
  ai-jobs-vps:/root/WB/app/arbitrage/scanner.py
ssh ai-jobs-vps "systemctl restart wb-bot.service"
```

### Force scan + show candidates
```bash
ssh ai-jobs-vps "sqlite3 /root/WB/data/app.db 'SELECT nm_id, market_price_rub, buyer_price_rub, listed_price_rub, margin_rub, ROUND(margin_percent, 1), ROUND(profit_per_ruble_day_pct, 2), spp_source FROM arb_candidates ORDER BY profit_per_ruble_day_pct DESC LIMIT 10'"
```

### Local tests
```bash
cd "/Users/refusned/Desktop/WB Price BOT" && /tmp/wb-venv/bin/python3 -m pytest tests/ -q
```

---

## 10. Quick reference

- **Текущая дата:** 2026-05-18 (Day 18)
- **Last commit:** `bb73094` Critical: fix double-СПП
- **Production:** active, scanner running, alerts working
- **Next session:** start with §8 above

### Файлы для resume завтра

1. **Этот файл** (`CLAUDE.md`) — модель ценообразования, статус, next steps
2. `app/arbitrage/margin.py` — формула margin (КРИТИЧНО — не ломай!)
3. `app/arbitrage/scanner.py` — main orchestrator
4. План: `/Users/refusned/.claude/plans/https-github-com-refusned-wb-price-track-whimsical-hopcroft.md`

### Ключевые числа для калибровки

| | Значение |
|---|---|
| RRC Станция Миди | 29 999₽ |
| Owner listed | 15 000₽ |
| WB API возвращает | 11 189₽ |
| Owner checkout | 10 658₽ |
| WB перечисляет | 11 000-12 000₽ |
| Margin / шт | ~891₽ (модель) ≈ 1 000₽ (reality) |
| Category СПП колонки | 24.4% (плавает 21-25%) |
| WB-Кошелёк bonus | 6% |
| Тариф commission FBS | ~16% Электроника |
| УСН | 2% |

**Если завтра scanner показывает странные числа** — verify против этой таблицы. Если listed_implied = 14 803 для арт 876392996, формула работает. Если что-то сильно отличается — кто-то сломал margin.py.
