# Календарное напоминание: Day 6 GATE

## КОГДА: примерно **2026-05-30** (через ~2 недели после 16 мая)

## Что нужно сделать тогда

### Шаг 1: проверить накопление данных

```bash
ssh ai-jobs-vps "cd /root/WB && /root/WB/.venv/bin/python -c \"
import asyncio, sys; sys.path.insert(0, '.')
async def m():
    from app.storage.db import Database
    from app.storage.missed_deal_repository import MissedDealRepository
    from app.storage.decision_snapshot_repository import DecisionSnapshotRepository
    db = Database('data/app.db'); await db.connect()
    md = MissedDealRepository(db)
    ds = DecisionSnapshotRepository(db)
    print('Missed-deal tags:', await md.count_tagged())
    print('Distribution:', await md.distribution())
    print('Decision snapshots:', await ds.count())
    print('Decision distribution:', await ds.distribution(days=30))
    await db.close()
asyncio.run(m())
\""
```

### Шаг 2: tag 10-15 missed deals

В Telegram → `/missed_deals` → пройти 10-15 candidates с inline-кнопками.

### Шаг 3: посмотреть распределение причин

| Преобладает | Выбираем направление |
|---|---|
| **cash** ≥ 40% | Direction A: Capital allocator (`/allocate`, sell-side liquidation) |
| **too_slow** ≥ 40% | Direction B: Speed pipeline (30-sec polling, one-tap-buy) |
| **bad_margin** ≥ 40% | Direction C: EV calibration (replace heuristic with EV formula) |
| Размыто | Default Direction C (lowest-risk, foundation для всех) |

### Шаг 4: проверить counterfactual signal

Decision snapshots должны показать:
- **total** alerted ≥ 10
- **by_action.bought ≥ 1** (если linking работает)
- Если 0 bought → бот алертит про невыгодные сделки → пересмотреть пороги

### Шаг 5: запустить аналитический baseline

```bash
ssh ai-jobs-vps "cd /root/WB && /root/WB/.venv/bin/python tools/analyze_baseline.py"
```

Прочитать отчёт. Посмотреть:
- `profit_per_ruble_day` по категориям
- `hold-time` распределение
- `missed-deal candidates count`

### Шаг 6: сказать мне результат

> "Распределение missed deals: cash=N, too_slow=M, bad_margin=K. Decision distribution: bought=X. Baseline shows ROI/day медиана Y для категории Z."

Я запущу Codex для реализации выбранного направления (9-12 дней работы).

---

## Что бот делает АВТОМАТИЧЕСКИ всё это время

| Что | Когда | Файлы/таблицы |
|---|---|---|
| Polling каталога WB | каждые 10 мин | `price_history` |
| Polling Seller API | каждые 30 мин | `own_orders`, `own_sales`, `own_stocks` |
| Auto-sync finance | раз в 24h (NEW Day 16) | `finance_journal` |
| Auto-collect СПП per category | раз в день | `personal_spp_snapshots` (source='auto_from_sales') |
| Decision_snapshot на каждом alert | при alert | `decision_snapshots` |
| Auto-detect stock arrivals → DM | каждые 30 мин при upsert | `pending_purchase_prompts` |
| Link decision → purchase | при `/buy` или auto-prompt reply | `decision_snapshots.purchase_id` |

## Что тебе делать руками всё это время

| Действие | Частота | Польза |
|---|---|---|
| `/setspp_log <%>` твоей buyer-СПП | 1x в неделю | Manual baseline для buyer-side СПП |
| Отвечать на DM "новая партия — цена?" | при отправке на FBS | Auto-purchases + lot ledger |
| Игнорировать всё остальное | — | — |

## Если что-то сломалось

```bash
# Health check
ssh ai-jobs-vps "systemctl status wb-bot.service --no-pager | head -10"

# Recent errors
ssh ai-jobs-vps "journalctl -u wb-bot.service --since '24 hours ago' | grep -iE 'ERROR|Traceback' | tail -20"

# Rollback (worst case)
ssh ai-jobs-vps "cd /root/WB && bash tools/rollback.sh"
```
