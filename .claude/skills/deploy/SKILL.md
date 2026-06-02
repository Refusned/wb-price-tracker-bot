---
name: deploy
description: Безопасная выкатка wb-price-tracker на прод (VPS wb-vps, развёрнут БЕЗ git — rsync локального дерева + рестарт wb-bot). Использовать когда Roman просит «задеплоить» / «выкатить». ВНИМАНИЕ: деплоится ЛОКАЛЬНОЕ дерево основного репо, не origin/main.
---

# deploy — выкатка wb-price-tracker на прод

VPS развёрнут БЕЗ git: `scripts/deploy.sh` (local-only, gitignored) rsync'ит
**локальное рабочее дерево основного репо** → `wb-vps:/root/WB` (`--delete`) +
`systemctl restart wb-bot`. Прод-состояние на сервере сохраняется: `.env`, `data/`
(БД + `.heartbeat`), `.venv` исключены из rsync.

⚠️ Уезжает дерево КАК ЕСТЬ, **не `origin/main`**. Перед деплоем основной репо должен
быть на нужном коммите (обычно `main`) и чистым (`git status`).

## Порядок (не пропускать)

1. **Pre-gate** (прод тратит реальные деньги и автопостит ответы покупателям):
   - Тесты зелёные: `.venv/bin/python -m pytest tests/ -q`
   - Secret-scan диффа (см. ниже) — ни токенов/JWT/ключей в коммитах.
   - `.env` (корень) — **НЕ читать/не светить**; он gitignored и исключён из rsync.
2. **В origin/main:** закоммитить (с `Co-Authored-By`), влить в `main` основного репо
   (ff-merge), `git push origin main`. Дерево основного репо → на `main`, чистое.
3. **Деплой:** `bash scripts/deploy.sh`.
4. **Post-verify** (ОБЯЗАТЕЛЬНО — без него тихое падение незаметно):
   - `ssh wb-vps "systemctl is-active wb-bot"` → `active`.
   - `ssh wb-vps "journalctl -u wb-bot --since '3 min ago' --no-pager"` → чистый старт
     («Bot polling started»), без `Traceback`/`ImportError`.
   - Профильный цикл реально отработал, напр.:
     `... | grep -iE 'Seller update|stocks=|SellerApiError|Error'`
     → `Seller update: ... stocks=N` без ошибок (для фич про остатки/заказы).

## Secret-scan диффа (своего secret_gate.sh пока нет — вручную)

```bash
git diff origin/main..HEAD | grep -nE '^\+' \
  | grep -iE 'eyJ[A-Za-z0-9_-]{10,}|bearer [A-Za-z0-9]{8,}|(token|api_key|secret|password) *= *["'"'"'][A-Za-z0-9]{16,}'
```
Пусто → чисто. Имена переменных (`WB_SELLER_API_KEY`, `getenv(...)`) — не секреты.

## Грабли (Got X)
- Деплоится **локальное дерево**, не origin/main. Забыл влить ветку в main / закоммитить
  → уедет старый или промежуточный код. Проверь `git -C <основной репо> log -1`.
- Работаешь в git-worktree (`.claude/worktrees/...`)? Деплой всё равно из ОСНОВНОГО репо
  (`deploy.sh` = `$(dirname)/..`). Сначала влей ветку в main основного репо.
- `--delete` в rsync: что не исключено и отсутствует локально — удалится на проде.
  `.env`/`data/`/`.venv` исключены (прод-БД и секреты целы). НЕ убирай эти exclude.
- `.claude/` исключён из rsync (скилы/worktrees на проде не нужны; без exclude на VPS
  уезжают гигабайты `.claude/worktrees/`). Сохрани exclude при копировании скрипта.
- Рестарт безопасен (graceful SIGTERM), но это прод — деплой только по явному запросу Романа.
