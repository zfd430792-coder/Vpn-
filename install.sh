#!/usr/bin/env bash
# anime-bot — установка одной командой.
#
#   curl -fsSL <raw-url>/install.sh | sudo bash
#
# Спросит токен, ID админа и данные юзербота. Вопросы читаются из /dev/tty,
# поэтому работает и когда скрипт пришёл по пайпу из curl.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/zfd430792-coder/Vpn-.git}"
REPO_BRANCH="${REPO_BRANCH:-claude/project-complete-removal-sd6wgq}"
INSTALL_DIR="${INSTALL_DIR:-/opt/anime-bot}"
ENV_DIR="${ENV_DIR:-/etc/anime-bot}"
DATA_DIR="${DATA_DIR:-/var/lib/anime-bot}"
SERVICE="${SERVICE:-anime-bot}"

B="\033[1m"; DIM="\033[2m"; OFF="\033[0m"
GRN="\033[1;32m"; RED="\033[1;31m"; CYN="\033[1;36m"; YLW="\033[1;33m"

STEP=0
TOTAL=7

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

askvar() {
  # askvar ПЕРЕМЕННАЯ "вопрос" [скрывать]
  local var="$1" prompt="$2" hidden="${3:-}" value="${!1:-}"
  if [[ -n "$value" ]]; then return; fi
  if [[ ! -t 0 && ! -e /dev/tty ]]; then
    die "Нужен ввод ($prompt), но терминала нет. Передай ${var}=... переменной окружения."
  fi
  while [[ -z "$value" ]]; do
    if [[ -n "$hidden" ]]; then
      printf "%b" "  ${CYN}?${OFF} ${prompt}: " > /dev/tty
      read -rs value < /dev/tty; echo > /dev/tty
    else
      printf "%b" "  ${CYN}?${OFF} ${prompt}: " > /dev/tty
      read -r value < /dev/tty
    fi
  done
  printf -v "$var" '%s' "$value"
}

banner
[[ "$(id -u)" -eq 0 ]] || die "Запускай под root: ${B}sudo bash install.sh${OFF}"

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

# --------------------------------------------------------------- 4. опрос
step "Настройки бота"
askvar BOT_TOKEN "Токен бота от @BotFather" hidden
askvar ADMINS    "Твой Telegram ID (узнать: @userinfobot)"
info "для юзербота нужны API_ID и API_HASH с my.telegram.org"
askvar API_ID    "API_ID"
askvar API_HASH  "API_HASH" hidden

[[ "$ADMINS" =~ ^[0-9,\ ]+$ ]] || die "ID админа — это число"
[[ "$API_ID"  =~ ^[0-9]+$   ]] || die "API_ID — это число"

# ----------------------------------------------------------------- 5. env
step "Пишу ${ENV_DIR}/env"
install -d -m 0700 "$ENV_DIR"
install -d -m 0755 "$DATA_DIR"
umask 077
cat > "$ENV_DIR/env" <<ENVEOF
BOT_TOKEN=$BOT_TOKEN
ADMINS=$ADMINS
API_ID=$API_ID
API_HASH=$API_HASH
SESSION=
STORAGE_CHANNEL=
CHANNEL_TITLE=${CHANNEL_TITLE:-Anime Storage}
DATA_DIR=$DATA_DIR
FREE_EPISODES=${FREE_EPISODES:-3}
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
cat > "/etc/systemd/system/${SERVICE}.service" <<UNITEOF
[Unit]
Description=anime-bot — каталог и выдача серий в Telegram
After=network-online.target
Wants=network-online.target

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
