#!/usr/bin/env bash
# Безопасный апдейтер WB-бота. Запускать из корня деплоя на сервере (напр. /root/WB):
#     bash deploy/update.sh [ref]      # ref по умолчанию = main
#
# Что делает:
#   1. Проверяет .env на ломающие изменения ДО рестарта (deny-by-default auth,
#      startup-check секрета) — чтобы бот не закрылся всем и не упал на старте.
#   2. Бэкапит БД (VACUUM INTO) и .env.
#   3. Обновляет код (fetch + checkout -B origin/<ref> — переживает переписанную
#      историю; .env и data/ не трогаются, они вне git).
#   4. Ставит зависимости в .venv (если есть; для docker — пропускает, сборка в образе).
#   5. Рестартит (systemd wb-bot.service ИЛИ docker compose — автоопределение).
#   6. Проверяет здоровье по data/.heartbeat; при провале — АВТО-ОТКАТ к прежнему коду.
set -euo pipefail

REF="${1:-main}"
SERVICE="wb-bot.service"
HEARTBEAT="data/.heartbeat"
TS="$(date +%Y%m%d-%H%M%S)"

log(){ printf '\033[1;34m[update]\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[update:FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

mtime(){ stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0; }

# ── 0. sanity ───────────────────────────────────────────────────────
[ -d .git ] || die "не git-репозиторий. cd в каталог деплоя (напр. /root/WB) и повтори."
command -v git >/dev/null || die "git не найден"
[ -f .env ] || die ".env отсутствует — бот без него не стартует"

# ── 1. .env guards: ловим ломающие изменения ДО рестарта ────────────
grep -Eq '^ALLOWED_USER_IDS=.+' .env \
  || die "ALLOWED_USER_IDS пуст → новый код закроет бота ВСЕМ (deny-by-default). Добавь: echo 'ALLOWED_USER_IDS=<твой_tg_id>' >> .env"
SHADOW="$(grep -E '^SHADOW_MODE=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]' | tr 'A-Z' 'a-z')"
SECRET="$(grep -E '^CALLBACK_SIGNING_SECRET=' .env | tail -1 | cut -d= -f2- | tr -d '[:space:]')"
if [ "$SHADOW" != "true" ] && [ -z "$SECRET" ]; then
  die "SHADOW_MODE!=true и CALLBACK_SIGNING_SECRET пуст → бот упадёт на старте (startup-check). Поставь SHADOW_MODE=true ИЛИ задай секрет: echo \"CALLBACK_SIGNING_SECRET=\$(openssl rand -hex 32)\" >> .env"
fi
log ".env guards пройдены"

# ── 2. бэкапы ───────────────────────────────────────────────────────
OLD_SHA="$(git rev-parse HEAD)"
log "текущий код: $OLD_SHA"
if [ -f data/app.db ]; then
  if command -v sqlite3 >/dev/null; then
    sqlite3 data/app.db "VACUUM INTO 'data/app.db.bak-$TS'" && log "БД → data/app.db.bak-$TS"
  else
    cp data/app.db "data/app.db.bak-$TS" && log "БД → data/app.db.bak-$TS (cp)"
  fi
fi
cp .env ".env.bak-$TS" && log ".env → .env.bak-$TS"
PRE_HB="$( [ -f "$HEARTBEAT" ] && mtime "$HEARTBEAT" || echo 0 )"

# ── restart / rollback (определяем до использования) ────────────────
RESTART_MODE=""
restart(){
  if systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE"; then
    RESTART_MODE="systemd"; systemctl restart "$SERVICE"
  elif [ -f docker-compose.yml ] || [ -f compose.yml ]; then
    RESTART_MODE="docker"; docker compose up -d --build 2>/dev/null || docker-compose up -d --build
  else
    die "не нашёл ни systemd $SERVICE, ни docker-compose.yml — не знаю, как рестартить"
  fi
}
rollback(){
  log "❌ деплой не прошёл — ОТКАТ к $OLD_SHA"
  git checkout -q "$OLD_SHA" || true
  restart || true
  die "откат выполнен. Логи: journalctl -u $SERVICE -n 60 --no-pager  (или docker compose logs --tail=60)"
}

# ── 3. обновление кода (переживает переписанную историю) ────────────
git fetch origin --quiet --prune || die "git fetch не прошёл"
git rev-parse --verify "origin/$REF" >/dev/null 2>&1 || die "origin/$REF не существует"
git checkout -q -B "$REF" "origin/$REF" || die "не удалось переключиться на origin/$REF (локальные правки в tracked-файлах? разберись вручную)"
NEW_SHA="$(git rev-parse HEAD)"
log "код обновлён: $OLD_SHA → $NEW_SHA"

# ── 4. зависимости ──────────────────────────────────────────────────
VENV=""
[ -d .venv ] && VENV=".venv"
[ -z "$VENV" ] && [ -d venv ] && VENV="venv"
if [ -n "$VENV" ]; then
  "./$VENV/bin/pip" install -q -r requirements.txt || rollback
  log "зависимости установлены ($VENV)"
else
  log "venv не найден — пропускаю pip (ок для docker: зависимости ставятся при сборке образа)"
fi

# ── 5. рестарт ──────────────────────────────────────────────────────
log "рестарт ($SERVICE / docker)..."
restart || rollback
if [ "$RESTART_MODE" = "systemd" ]; then
  sleep 3
  systemctl is-active --quiet "$SERVICE" || rollback   # упал на старте → откат сразу
fi

# ── 6. health-check по heartbeat (его пишет новый scheduler) ────────
log "жду свежий heartbeat (до ~120с)..."
ok=0
for _ in $(seq 1 24); do
  if [ -f "$HEARTBEAT" ] && [ "$(mtime "$HEARTBEAT")" -gt "$PRE_HB" ]; then ok=1; break; fi
  sleep 5
done
[ "$ok" = 1 ] || rollback

log "✅ деплой $NEW_SHA жив (heartbeat свежий)."
log "Проверь в Telegram: отправь боту /status"
