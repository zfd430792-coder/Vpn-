#!/usr/bin/env bash
# anime-bot — установка одной командой.
#
#   curl -fsSL <raw-url>/install.sh | sudo bash
#
# Спросит токен, ID админа и данные юзербота. Вопросы читаются из /dev/tty,
# поэтому работает и когда скрипт пришёл по пайпу из curl.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/zfd430792-coder/Vpn-.git}"
REPO_BRANCH="${REPO_BRANCH:-claude/traffic-consuming-bot-iuxyrf}"
INSTALL_DIR="${INSTALL_DIR:-/opt/anime-bot}"
ENV_DIR="${ENV_DIR:-/etc/anime-bot}"
DATA_DIR="${DATA_DIR:-/var/lib/anime-bot}"
SERVICE="${SERVICE:-anime-bot}"

B="\033[1m"; DIM="\033[2m"; OFF="\033[0m"
GRN="\033[1;32m"; RED="\033[1;31m"; CYN="\033[1;36m"; YLW="\033[1;33m"

STEP=0
CONFIG_ONLY="${CONFIG_ONLY:-0}"
TOTAL=7
[[ "$CONFIG_ONLY" == "1" ]] && TOTAL=4 || true

banner() {
  printf "%b" "${CYN}
   ╭──────────────────────────────────────────────╮
   │                                              │
   │     ${B}anime-bot${OFF}${CYN}  ·  установка за один заход    │
   │                                              │
   ╰──────────────────────────────────────────────╯${OFF}

"
}

step()  { STEP=$((STEP+1)); printf "%b\n" "${GRN}[${STEP}/${TOTAL}]${OFF} ${B}$*${OFF}"; }
info()  { printf "%b\n" "      ${DIM}$*${OFF}"; }
warn()  { printf "%b\n" "  ${YLW}!!${OFF} $*"; }
die()   { printf "%b\n" "  ${RED}!!${OFF} $*" >&2; exit 1; }

# Вставка в терминал приносит мусор: маркеры bracketed-paste, \r из
# Windows-буфера, управляющие символы, кавычки. Всё это молча попадало
# в токен и ломало установку — поэтому чистим агрессивно.
sanitize() {
  local s="$1"
  s="${s//$'\e'\[200~/}"
  s="${s//$'\e'\[201~/}"
  s="${s//$'\r'/}"
  s="$(printf '%s' "$s" | tr -d '\000-\037\177')"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  s="${s#[\"\']}"
  s="${s%[\"\']}"
  printf '%s' "$s"
}

# Из BotFather токен обычно копируют вместе с текстом вокруг — выдираем сам токен.
extract_token() { printf '%s' "$1" | grep -oE '[0-9]{6,12}:[A-Za-z0-9_-]{30,}' | head -1; }
extract_digits() { printf '%s' "$1" | grep -oE '[0-9]{4,}' | head -1; }
extract_hash()   { printf '%s' "$1" | grep -oE '[0-9a-fA-F]{32}' | head -1; }

mask() {
  local s="$1" n=${#1}
  if (( n <= 12 )); then printf '%s' "${s:0:3}***"; else printf '%s…%s' "${s:0:10}" "${s: -4}"; fi
}

# Спрашивает значение, пока оно не пройдёт проверку. Не падает на мусоре.
# ask_value ПЕРЕМЕННАЯ "вопрос" очиститель проверка "подсказка при ошибке"
ask_value() {
  local var="$1" prompt="$2" cleaner="$3" validator="$4" hint="$5"
  local value raw clean
  value="$(sanitize "${!var:-}")"

  # значение пришло из окружения — проверяем и не спрашиваем
  if [[ -n "$value" ]]; then
    clean="$($cleaner "$value")"
    if [[ -n "$clean" ]] && $validator "$clean"; then
      printf -v "$var" '%s' "$clean"
      info "$prompt: $(mask "$clean")  ${DIM}(из окружения)${OFF}"
      return
    fi
    warn "$prompt из окружения не подошёл — спрошу заново"
  fi

  [[ -e /dev/tty ]] || die "Нужен ввод ($prompt), но терминала нет. Передай ${var}=... переменной окружения."

  local tries=0
  while :; do
    tries=$((tries+1))
    (( tries > 5 )) && die "Пять раз не вышло. Запусти установщик заново."
    printf "%b" "  ${CYN}?${OFF} ${prompt}: " > /dev/tty
    IFS= read -r raw < /dev/tty || die "Ввод прерван"
    clean="$($cleaner "$(sanitize "$raw")")"
    if [[ -z "$clean" ]] || ! $validator "$clean"; then
      printf "%b\n" "  ${RED}!!${OFF} ${hint}" > /dev/tty
      continue
    fi
    printf -v "$var" '%s' "$clean"
    printf "%b\n" "  ${GRN}ok${OFF} принято: $(mask "$clean")" > /dev/tty
    return
  done
}

as_is() { printf '%s' "$1"; }
valid_token()  { [[ "$1" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]]; }
valid_id()     { [[ "$1" =~ ^[0-9]{5,}$ ]]; }
valid_apiid()  { [[ "$1" =~ ^[0-9]{4,}$ ]]; }
valid_hash()   { [[ "$1" =~ ^[0-9a-fA-F]{32}$ ]]; }

# Спрашивает у Telegram, живой ли токен. Сеть недоступна — только предупреждаем.
check_token() {
  local token="$1" out
  out="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${token}/getMe" 2>/dev/null || true)"
  if [[ -z "$out" ]]; then
    warn "не достучался до api.telegram.org — токен проверю позже, при запуске"
    return 0
  fi
  if [[ "$out" != *'"ok":true'* ]]; then
    return 1
  fi
  BOT_USERNAME="$(printf '%s' "$out" | grep -oE '"username":"[^"]+"' | head -1 | cut -d'"' -f4)"
  return 0
}

banner
[[ "$(id -u)" -eq 0 ]] || die "Запускай под root: ${B}sudo bash install.sh${OFF}"

if [[ "$CONFIG_ONLY" != "1" ]]; then
  # ---------------------------------------------------------------- 1. пакеты
  step "Ставлю системные пакеты"
  if   command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates >/dev/null
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3 python3-pip git curl ca-certificates >/dev/null
  elif command -v yum >/dev/null 2>&1; then yum install -y -q python3 python3-pip git curl ca-certificates >/dev/null
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache -q python3 py3-pip git curl ca-certificates bash >/dev/null
  else die "Не понял, какой тут пакетный менеджер"; fi
  info "python $(python3 --version 2>&1 | awk '{print $2}')"

  # ------------------------------------------------------------------ 2. код
  step "Забираю код в ${INSTALL_DIR}"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH" -q
    git -C "$INSTALL_DIR" checkout -q -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
    info "обновил существующую копию"
  else
    git clone -q --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    info "склонировал свежую"
  fi

  # --------------------------------------------------------------- 3. питон
  step "Собираю виртуальное окружение"
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
  info "aiogram + telethon на месте"
else
  info "режим перенастройки: пакеты, код и venv не трогаю"
  [[ -d "$INSTALL_DIR/.venv" ]] || die "Установки в $INSTALL_DIR нет — запусти без CONFIG_ONLY"
fi

# --------------------------------------------------------------- 4. опрос
step "Настройки бота"
info "вставлять можно прямо с текстом вокруг — нужное выдерну сам"
BOT_USERNAME=""
while :; do
  ask_value BOT_TOKEN "Токен бота от @BotFather" extract_token valid_token \
    "не похоже на токен. Он выглядит так: 123456789:AAE_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
  if check_token "$BOT_TOKEN"; then break; fi
  warn "Telegram этот токен не принял. Проверь, тот ли бот и не отозван ли токен."
  BOT_TOKEN=""
done
[[ -n "${BOT_USERNAME:-}" ]] && info "бот на связи: @${BOT_USERNAME}" || true

ask_value ADMINS "Твой Telegram ID (узнать: @userinfobot)" extract_digits valid_id \
  "нужен числовой ID, а не @имя — напиши @userinfobot, он пришлёт"

info "API_ID и API_HASH берутся на my.telegram.org → API development tools"
ask_value API_ID   "API_ID"   extract_digits valid_apiid "API_ID — это число"
ask_value API_HASH "API_HASH" extract_hash   valid_hash  "API_HASH — 32 знака: цифры и буквы a–f"

# ----------------------------------------------------------------- 5. env
step "Пишу ${ENV_DIR}/env"
# Повторный запуск не должен стирать то, что уже получено: иначе юзербот
# пришлось бы логинить заново, а канал создался бы второй раз.
OLD_SESSION=""; OLD_CHANNEL=""
if [[ -f "$ENV_DIR/env" ]]; then
  OLD_SESSION="$(sed -n 's/^SESSION=//p'         "$ENV_DIR/env" | head -1)"
  OLD_CHANNEL="$(sed -n 's/^STORAGE_CHANNEL=//p' "$ENV_DIR/env" | head -1)"
  [[ -n "$OLD_SESSION" ]] && info "сессию юзербота сохраняю" || true
  [[ -n "$OLD_CHANNEL" ]] && info "канал-хранилище сохраняю: $OLD_CHANNEL" || true
fi
install -d -m 0700 "$ENV_DIR"
install -d -m 0755 "$DATA_DIR"
umask 077
cat > "$ENV_DIR/env" <<ENVEOF
BOT_TOKEN=$BOT_TOKEN
ADMINS=$ADMINS
API_ID=$API_ID
API_HASH=$API_HASH
SESSION=$OLD_SESSION
STORAGE_CHANNEL=$OLD_CHANNEL
CHANNEL_TITLE=${CHANNEL_TITLE:-Anime Storage}
DATA_DIR=$DATA_DIR
TRIAL_ENABLED=${TRIAL_ENABLED:-1}
TRIAL_DAYS=${TRIAL_DAYS:-3}
PROTECT_CONTENT=${PROTECT_CONTENT:-1}
AUTODELETE=${AUTODELETE:-0}
DELIVERY=${DELIVERY:-bot}
ENVEOF
chmod 600 "$ENV_DIR/env"
info "права 600 — токены внутри"

# ------------------------------------------------------- 6. вход юзербота
step "Вход юзербота и создание канала"
info "сейчас Telegram пришлёт код на аккаунт — введи его"
echo
set +e
( set -a; . "$ENV_DIR/env"; set +a; \
  ENV_FILE="$ENV_DIR/env" "$INSTALL_DIR/.venv/bin/python" -m anibot.setup ) < /dev/tty
SETUP_RC=$?
set -e
echo
if [[ $SETUP_RC -ne 0 ]]; then
  warn "Юзербот не настроен. Бот запустится, но канал надо будет завести руками:"
  warn "  cd $INSTALL_DIR && ENV_FILE=$ENV_DIR/env .venv/bin/python -m anibot.setup"
fi

# ------------------------------------------------------------- 7. systemd
step "Создаю сервис ${SERVICE}"

# Скрипт-тревога: его дёргает systemd, когда сервис падает. Сам бот в этот
# момент уже мёртв и написать не может, поэтому шлём через curl.
cat > /usr/local/bin/anime-bot-alert <<'ALERTEOF'
#!/usr/bin/env bash
set -u
ENV_FILE="${ENV_FILE:-/etc/anime-bot/env}"
[[ -f "$ENV_FILE" ]] || exit 0
set -a; . "$ENV_FILE"; set +a
[[ -n "${BOT_TOKEN:-}" ]] || exit 0

UNIT="${1:-anime-bot.service}"
LOG="$(journalctl -u "$UNIT" -n 12 --no-pager 2>/dev/null | tail -12)"
TEXT="🔴 Сервис $UNIT упал
$(date '+%d.%m %H:%M')
Хост: $(hostname)

Последние строки лога:
$LOG"

send() {  # send <чат> [тема]
  local chat="$1" thread="${2:-}"
  local args=(--data-urlencode "chat_id=$chat" --data-urlencode "text=$TEXT")
  [[ -n "$thread" ]] && args+=(--data-urlencode "message_thread_id=$thread")
  curl -s --max-time 15 -X POST     "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" "${args[@]}" >/dev/null || true
}

if [[ -n "${LOG_ERRORS:-}" ]]; then
  send "${LOG_ERRORS%%:*}" "$(printf '%s' "${LOG_ERRORS#*:}" | grep -E '^[0-9]+$' || true)"
fi
IFS=',' read -ra IDS <<< "${ADMINS:-}"
for id in "${IDS[@]}"; do
  id="${id// /}"
  [[ -n "$id" ]] && send "$id"
done
ALERTEOF
chmod +x /usr/local/bin/anime-bot-alert

cat > "/etc/systemd/system/${SERVICE}-alert@.service" <<ALERTUNIT
[Unit]
Description=Сообщить админам, что %i упал

[Service]
Type=oneshot
Environment=ENV_FILE=$ENV_DIR/env
ExecStart=/usr/local/bin/anime-bot-alert %i
ALERTUNIT

cat > "/etc/systemd/system/${SERVICE}.service" <<UNITEOF
[Unit]
Description=anime-bot — каталог и выдача серий в Telegram
After=network-online.target
Wants=network-online.target
OnFailure=${SERVICE}-alert@%n.service

[Service]
Type=simple
EnvironmentFile=$ENV_DIR/env
Environment=ENV_FILE=$ENV_DIR/env
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m anibot
Restart=always
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable --now "${SERVICE}.service" >/dev/null 2>&1

sleep 3
echo
if systemctl is-active --quiet "${SERVICE}.service"; then
  CHANNEL="$(grep -E '^STORAGE_CHANNEL=' "$ENV_DIR/env" | cut -d= -f2)"
  printf "%b" "${GRN}
   ╭──────────────────────────────────────────────╮
   │            ${B}готово, бот работает${OFF}${GRN}              │
   ╰──────────────────────────────────────────────╯${OFF}

   ${B}Хранилище:${OFF}  ${CHANNEL:-не настроено}
   ${B}Логи:${OFF}       journalctl -u ${SERVICE} -f
   ${B}Рестарт:${OFF}    systemctl restart ${SERVICE}
   ${B}Настройки:${OFF}  ${ENV_DIR}/env

   ${DIM}Открой бота, нажми /start — там будет кнопка «Админка».
   Заливай серии в канал с подписью:${OFF}
       Название: Моё Аниме
       Сезон: 1
       Серия: 7
       Озвучка: Studio Band

"
else
  die "Сервис не поднялся. Смотри: journalctl -u ${SERVICE} -n 50 --no-pager"
fi
