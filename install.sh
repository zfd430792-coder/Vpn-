#!/usr/bin/env bash
# voice-bot — установка одной командой.
#
#   curl -fsSL <raw-url>/install.sh | sudo bash
#
# Спросит только токен бота. Вопросы читаются из /dev/tty, поэтому работает
# и когда скрипт пришёл по пайпу из curl. Повторный запуск обновляет код и
# перезапускает сервис, сохранённый токен заново не спрашивает.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/zfd430792-coder/Vpn-.git}"
REPO_BRANCH="${REPO_BRANCH:-claude/traffic-consuming-bot-iuxyrf}"
INSTALL_DIR="${INSTALL_DIR:-/opt/voice-bot}"
ENV_DIR="${ENV_DIR:-/etc/voice-bot}"
SERVICE="${SERVICE:-voice-bot}"

B="\033[1m"; DIM="\033[2m"; OFF="\033[0m"
GRN="\033[1;32m"; RED="\033[1;31m"; CYN="\033[1;36m"; YLW="\033[1;33m"

STEP=0
TOTAL=5

banner() {
  printf "%b" "${CYN}
   ╭──────────────────────────────────────────────╮
   │                                              │
   │     ${B}voice-bot${OFF}${CYN}  ·  чей телефон по голосовому  │
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
valid_token()   { [[ "$1" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]]; }

mask() {
  local s="$1" n=${#1}
  if (( n <= 12 )); then printf '%s' "${s:0:3}***"; else printf '%s…%s' "${s:0:10}" "${s: -4}"; fi
}

# Спрашивает токен, пока он не будет похож на токен. Не падает на мусоре.
ask_token() {
  local raw clean
  clean="$(extract_token "$(sanitize "${BOT_TOKEN:-}")" || true)"
  if [[ -n "$clean" ]] && valid_token "$clean"; then
    BOT_TOKEN="$clean"
    info "токен: $(mask "$clean")  ${DIM}(${TOKEN_FROM:-из окружения})${OFF}"
    return
  fi
  [[ -n "${BOT_TOKEN:-}" ]] && warn "токен из окружения не подошёл — спрошу заново"

  [[ -e /dev/tty ]] || die "Нужен токен, но терминала нет. Передай BOT_TOKEN=... переменной окружения."
  local tries=0
  while :; do
    tries=$((tries+1))
    (( tries > 5 )) && die "Пять раз не вышло. Запусти установщик заново."
    printf "%b" "  ${CYN}?${OFF} Токен бота от @BotFather: " > /dev/tty
    IFS= read -r raw < /dev/tty || die "Ввод прерван"
    clean="$(extract_token "$(sanitize "$raw")" || true)"
    if [[ -z "$clean" ]] || ! valid_token "$clean"; then
      printf "%b\n" "  ${RED}!!${OFF} не похоже на токен. Он выглядит так: 123456789:AAE_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" > /dev/tty
      continue
    fi
    BOT_TOKEN="$clean"
    printf "%b\n" "  ${GRN}ok${OFF} принято: $(mask "$clean")" > /dev/tty
    return
  done
}

# Спрашивает у Telegram, живой ли токен. Сеть недоступна — только предупреждаем.
check_token() {
  local out
  out="$(curl -fsS --max-time 15 "https://api.telegram.org/bot${BOT_TOKEN}/getMe" 2>/dev/null || true)"
  if [[ -z "$out" ]]; then
    warn "не достучался до api.telegram.org — токен проверю позже, при запуске"
    return 0
  fi
  [[ "$out" == *'"ok":true'* ]] || return 1
  BOT_USERNAME="$(printf '%s' "$out" | grep -oE '"username":"[^"]+"' | head -1 | cut -d'"' -f4 || true)"
  return 0
}

banner
[[ "$(id -u)" -eq 0 ]] || die "Запускай под root: ${B}sudo bash install.sh${OFF}"

# ------------------------------------------------------------------ 1. пакеты
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

# --------------------------------------------------------------------- 2. код
step "Забираю код в ${INSTALL_DIR}"
if [[ -d "$INSTALL_DIR/.git" ]]; then
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH" -q
  git -C "$INSTALL_DIR" checkout -q -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
  info "обновил существующую копию"
else
  git clone -q --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  info "склонировал свежую"
fi

# ------------------------------------------------------------------- 3. питон
step "Собираю виртуальное окружение"
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
info "aiogram на месте"

# ------------------------------------------------------------------- 4. токен
step "Токен бота"
# Повторный запуск — это обновление: токен уже лежит в env, не спрашиваем.
if [[ -z "${BOT_TOKEN:-}" && -f "$ENV_DIR/env" ]]; then
  BOT_TOKEN="$(sed -n 's/^BOT_TOKEN=//p' "$ENV_DIR/env" | head -1)"
  TOKEN_FROM="из ${ENV_DIR}/env"
fi
info "вставлять можно прямо с текстом от @BotFather — токен выдерну сам"
BOT_USERNAME=""
while :; do
  ask_token
  if check_token; then break; fi
  warn "Telegram этот токен не принял. Проверь, тот ли бот и не отозван ли токен."
  BOT_TOKEN=""
done
[[ -n "$BOT_USERNAME" ]] && info "бот на связи: @${BOT_USERNAME}" || true

install -d -m 0700 "$ENV_DIR"
( umask 077; printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN" > "$ENV_DIR/env" )
chmod 600 "$ENV_DIR/env"
info "записал в ${ENV_DIR}/env, права 600"

# ----------------------------------------------------------------- 5. systemd
step "Запускаю сервис ${SERVICE}"
cat > "/etc/systemd/system/${SERVICE}.service" <<UNITEOF
[Unit]
Description=voice-bot — с какого устройства записано голосовое
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_DIR/env
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m voicebot
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNITEOF
systemctl daemon-reload
systemctl enable "${SERVICE}.service" >/dev/null 2>&1
systemctl restart "${SERVICE}.service"

sleep 3
echo
if systemctl is-active --quiet "${SERVICE}.service"; then
  printf "%b" "${GRN}
   ╭──────────────────────────────────────────────╮
   │            ${B}готово, бот работает${OFF}${GRN}              │
   ╰──────────────────────────────────────────────╯${OFF}

   ${B}Бот:${OFF}        ${BOT_USERNAME:+https://t.me/}${BOT_USERNAME:-тот, чей токен вставил}
   ${B}Логи:${OFF}       journalctl -u ${SERVICE} -f
   ${B}Рестарт:${OFF}    systemctl restart ${SERVICE}
   ${B}Обновить:${OFF}   запусти эту же команду ещё раз

   ${DIM}Перешли боту любое голосовое — ответит, с какого устройства оно.${OFF}

"
else
  die "Сервис не поднялся. Смотри: journalctl -u ${SERVICE} -n 50 --no-pager"
fi
