#!/usr/bin/env bash
# Разовая настройка: репозиторий на GitHub, секреты, первый запуск.
# Запуск:  cd ~/Documents/china-tech-bot && ./setup.sh
# Токены вводятся здесь, в вашем терминале, и никуда больше не попадают.

set -euo pipefail

say()  { printf "\n\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$*"; }
die()  { bad "$*"; exit 1; }

tg() { curl -sS --max-time 20 "https://api.telegram.org/bot${BOT_TOKEN}/$1" ${2:+--data-urlencode "$2"}; }
json_str() { grep -o "\"$1\":\"[^\"]*\"" | head -1 | cut -d'"' -f4; }

cd "$(dirname "$0")"

# Папку готовил Claude с ограниченными правами — git мог оставить файлы-замки.
find .git -name "*.lock" -delete 2>/dev/null || true
find .git/objects -name "tmp_obj_*" -delete 2>/dev/null || true

# ── 0. gh ────────────────────────────────────────────────────────────────────
say "1/7  Проверяю GitHub CLI"
if ! command -v gh >/dev/null; then
  if command -v brew >/dev/null; then
    echo "  gh не найден, ставлю через Homebrew…"
    brew install gh
  else
    die "Нужен gh. Поставьте Homebrew (brew.sh), потом: brew install gh"
  fi
fi
ok "gh на месте"

if ! gh auth status >/dev/null 2>&1; then
  say "Вход в GitHub — откроется браузер"
  gh auth login -w -s repo,workflow
fi
GH_USER=$(gh api user --jq .login)
ok "GitHub: $GH_USER"

# ── 1. токен бота ────────────────────────────────────────────────────────────
say "2/7  Токен бота"
read -rsp "  Вставьте токен от @BotFather: " BOT_TOKEN; echo
BOT_INFO=$(tg getMe) || die "Telegram недоступен"
echo "$BOT_INFO" | grep -q '"ok":true' || die "Токен не принят Telegram"
BOT_NAME=$(echo "$BOT_INFO" | json_str username)
ok "Бот @${BOT_NAME}"

# ── 2. ваш chat_id ───────────────────────────────────────────────────────────
say "3/7  Ваш chat_id (сюда будут падать посты на модерацию)"
echo "  Откройте @${BOT_NAME} в Telegram и отправьте ему /start."
read -rp "  Отправили? Enter — продолжить: " _
CHAT_ID=$(tg getUpdates | grep -o '"chat":{"id":-\?[0-9]*' | head -1 | grep -o '\-\?[0-9]*$' || true)
if [ -z "${CHAT_ID:-}" ]; then
  echo "  Не удалось определить автоматически."
  read -rp "  Введите chat_id вручную: " CHAT_ID
fi
ok "chat_id: $CHAT_ID"

# ── 3. канал ─────────────────────────────────────────────────────────────────
say "4/7  Канал"
echo "  Создайте канал и добавьте @${BOT_NAME} администратором с правом публиковать."
echo "  Для приватного канала: перешлите любой пост из него боту и посмотрите /id."
read -rp "  Введите @username канала (или -100… для приватного): " CHANNEL_ID
CHAT_INFO=$(tg getChat "chat_id=${CHANNEL_ID}")
if echo "$CHAT_INFO" | grep -q '"ok":true'; then
  ok "Канал найден: $(echo "$CHAT_INFO" | json_str title)"
  TEST=$(curl -sS --max-time 20 "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        --data-urlencode "chat_id=${CHANNEL_ID}" \
        --data-urlencode "text=Бот подключён. Это тестовое сообщение, можно удалить.")
  echo "$TEST" | grep -q '"ok":true' && ok "Тестовый пост отправлен" \
    || bad "Бот не может писать в канал — проверьте, что он админ (настройку продолжаю)"
else
  bad "Канал не найден — впишете TELEGRAM_CHANNEL_ID позже в секретах (настройку продолжаю)"
fi

# ── 4. DeepSeek ──────────────────────────────────────────────────────────────
say "5/7  Ключ DeepSeek"
read -rsp "  Вставьте ключ (sk-…): " DS_KEY; echo
DS_RESP=$(curl -sS --max-time 40 https://api.deepseek.com/chat/completions \
  -H "Authorization: Bearer ${DS_KEY}" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","max_tokens":5,"messages":[{"role":"user","content":"ping"}]}')
if echo "$DS_RESP" | grep -q '"choices"'; then
  ok "Ключ работает"
else
  bad "DeepSeek ответил: $(echo "$DS_RESP" | head -c 200)"
  read -rp "  Продолжить всё равно? [y/N] " a; [ "${a:-n}" = "y" ] || exit 1
fi

# ── 5. репозиторий ───────────────────────────────────────────────────────────
say "6/7  Репозиторий"
if [ ! -d .git ]; then git init -q && git branch -M main; fi
git add -A
git diff --staged --quiet || git commit -qm "china tech bot: первичная настройка"
if gh repo view china-tech-bot >/dev/null 2>&1; then
  ok "Репозиторий уже существует"
  git remote get-url origin >/dev/null 2>&1 || \
    git remote add origin "https://github.com/${GH_USER}/china-tech-bot.git"
  git push -u origin main
else
  gh repo create china-tech-bot --private --source=. --push
  ok "Создан приватный ${GH_USER}/china-tech-bot"
fi

# ── 6. секреты и права ───────────────────────────────────────────────────────
say "7/7  Секреты и запуск"
printf '%s' "$BOT_TOKEN"  | gh secret set TELEGRAM_BOT_TOKEN
printf '%s' "$CHANNEL_ID" | gh secret set TELEGRAM_CHANNEL_ID
printf '%s' "$CHAT_ID"    | gh secret set MODERATOR_CHAT_ID
printf '%s' "$DS_KEY"     | gh secret set DEEPSEEK_API_KEY
ok "Четыре секрета записаны"

gh api -X PUT "repos/${GH_USER}/china-tech-bot/actions/permissions/workflow" \
  -f default_workflow_permissions=write -F can_approve_pull_request_reviews=false >/dev/null
ok "Actions разрешено писать в репозиторий"

sleep 3
gh workflow run collect.yml >/dev/null 2>&1 && ok "Первый сбор запущен" \
  || bad "Запустите вручную: gh workflow run collect.yml"

say "Готово."
echo "  Через пару минут первые посты придут в личку от @${BOT_NAME}."
echo "  Логи:      gh run watch"
echo "  Проверить: gh run list --limit 5"
