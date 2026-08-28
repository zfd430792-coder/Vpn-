#!/usr/bin/env bash
# One-shot installer for vpn-traffic-bot.
#
# Usage (as root):
#   TELEGRAM_BOT_TOKEN='123:AA...' SUB_HWID='xxxx' bash install.sh
# or one-liner:
#   curl -fsSL https://raw.githubusercontent.com/zfd430792-coder/Vpn-/claude/traffic-consuming-bot-iuxyrf/install.sh \
#     | sudo TELEGRAM_BOT_TOKEN='123:AA...' SUB_HWID='xxxx' bash

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/zfd430792-coder/Vpn-.git}"
REPO_BRANCH="${REPO_BRANCH:-claude/traffic-consuming-bot-iuxyrf}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vpn-traffic-bot}"
ENV_DIR="${ENV_DIR:-/etc/vpn-traffic-bot}"
SERVICE_NAME="${SERVICE_NAME:-vpn-traffic-bot}"
WORKERS="${WORKERS:-32}"
DEFAULT_LIMIT="${DEFAULT_LIMIT:-0}"
SOCKS_PORT="${SOCKS_PORT:-10808}"
SUB_HWID="${SUB_HWID:-}"
SUB_UA="${SUB_UA:-Happ/1.11.1}"
SB_FALLBACK_VER="${SB_FALLBACK_VER:-1.11.15}"

msg() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

if [[ "$(id -u)" -ne 0 ]]; then
  err "нужно запускать под root (sudo)."
  exit 1
fi

if [[ -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then
  err "TELEGRAM_BOT_TOKEN не задан. Пример: TELEGRAM_BOT_TOKEN='123:AA...' SUB_HWID='xxxx' bash install.sh"
  exit 1
fi

install_deps() {
  msg "ставлю базовые пакеты"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip git curl ca-certificates tar
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip git curl ca-certificates tar
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 python3-pip git curl ca-certificates tar
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 py3-pip git curl ca-certificates tar bash
  else
    err "неизвестный менеджер пакетов. поставь python3, git, curl вручную и запусти заново."
    exit 1
  fi
}

install_singbox() {
  if command -v sing-box >/dev/null 2>&1; then
    msg "sing-box уже стоит: $(sing-box version 2>/dev/null | head -1 || echo unknown)"
    return
  fi
  msg "ставлю sing-box"
  local arch
  case "$(uname -m)" in
    x86_64|amd64)   arch=amd64 ;;
    aarch64|arm64)  arch=arm64 ;;
    armv7l)         arch=armv7 ;;
    *) err "неподдерживаемая архитектура $(uname -m)"; exit 1 ;;
  esac
  local ver
  ver="$(curl -fsSL 'https://api.github.com/repos/SagerNet/sing-box/releases/latest' \
        | sed -n 's/.*\"tag_name\":[[:space:]]*\"v\([^\"]*\)\".*/\1/p' | head -1 || true)"
  if [[ -z "$ver" ]]; then
    ver="$SB_FALLBACK_VER"
    msg "GitHub API недоступен, беру ${ver}"
  fi
  local url="https://github.com/SagerNet/sing-box/releases/download/v${ver}/sing-box-${ver}-linux-${arch}.tar.gz"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL "$url" -o "$tmp/sb.tar.gz"
  tar -xzf "$tmp/sb.tar.gz" -C "$tmp"
  install -m 0755 "$tmp/sing-box-${ver}-linux-${arch}/sing-box" /usr/local/bin/sing-box
  rm -rf "$tmp"
  msg "sing-box установлен: $(sing-box version | head -1)"
}

fetch_repo() {
  msg "готовлю $INSTALL_DIR"
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" checkout -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
  else
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

setup_venv() {
  msg "делаю venv и ставлю зависимости"
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
}

write_env() {
  msg "пишу $ENV_DIR/env (root:600)"
  install -d -m 0700 "$ENV_DIR"
  umask 077
  cat > "$ENV_DIR/env" <<ENV
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
WORKERS=$WORKERS
DEFAULT_LIMIT=$DEFAULT_LIMIT
SOCKS_PORT=$SOCKS_PORT
SUB_HWID=$SUB_HWID
SUB_UA=$SUB_UA
SINGBOX_BIN=/usr/local/bin/sing-box
ENV
  chmod 0600 "$ENV_DIR/env"
}

write_service() {
  msg "пишу systemd unit"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=VPN traffic-burning Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_DIR/env
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m bot.tg
Restart=always
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}.service"
}

install_deps
install_singbox
fetch_repo
setup_venv
write_env
write_service

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  msg "бот запущен."
  msg "лог:    journalctl -u ${SERVICE_NAME} -f"
  msg "статус: systemctl status ${SERVICE_NAME}"
  msg "стоп:   systemctl stop ${SERVICE_NAME}"
else
  err "сервис не поднялся, смотри: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"
  exit 1
fi
