#!/usr/bin/env bash
# Installer for vpn-traffic-bot. Roles: bot (default) | agent.
set -euo pipefail

ROLE="${ROLE:-bot}"
REPO_URL="${REPO_URL:-https://github.com/zfd430792-coder/Vpn-.git}"
REPO_BRANCH="${REPO_BRANCH:-claude/traffic-consuming-bot-iuxyrf}"
INSTALL_DIR="${INSTALL_DIR:-/opt/vpn-traffic-bot}"
ENV_DIR="${ENV_DIR:-/etc/vpn-traffic-bot}"
DATA_DIR="${DATA_DIR:-/var/lib/vpn-traffic-bot}"
WORKERS="${WORKERS:-64}"
DEFAULT_LIMIT="${DEFAULT_LIMIT:-0}"
SOCKS_PORT="${SOCKS_PORT:-10808}"
SUB_HWID="${SUB_HWID:-}"
SUB_UA="${SUB_UA:-Happ/1.11.1}"
AGENT_PORT="${AGENT_PORT:-8787}"
SB_FALLBACK_VER="${SB_FALLBACK_VER:-1.11.15}"

if [[ "$ROLE" == "agent" ]]; then
  SERVICE_NAME="${SERVICE_NAME:-vpn-traffic-agent}"
  AGENT_TOKEN="${AGENT_TOKEN:-$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
else
  SERVICE_NAME="${SERVICE_NAME:-vpn-traffic-bot}"
fi

msg() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; }

if [[ "$(id -u)" -ne 0 ]]; then err "нужно под root (sudo)."; exit 1; fi
if [[ "$ROLE" == "bot" && -z "${TELEGRAM_BOT_TOKEN:-}" ]]; then err "TELEGRAM_BOT_TOKEN не задан."; exit 1; fi

install_deps() {
  msg "ставлю пакеты"
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive; apt-get update -y
    apt-get install -y --no-install-recommends python3 python3-venv python3-pip git curl ca-certificates tar
  elif command -v dnf >/dev/null 2>&1; then dnf install -y python3 python3-pip git curl ca-certificates tar
  elif command -v yum >/dev/null 2>&1; then yum install -y python3 python3-pip git curl ca-certificates tar
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache python3 py3-pip git curl ca-certificates tar bash
  else err "неизвестный пакетный менеджер"; exit 1; fi
}
install_singbox() {
  command -v sing-box >/dev/null 2>&1 && { msg "sing-box есть"; return; }
  msg "ставлю sing-box"
  local arch
  case "$(uname -m)" in
    x86_64|amd64) arch=amd64 ;; aarch64|arm64) arch=arm64 ;; armv7l) arch=armv7 ;;
    *) err "архитектура $(uname -m) не поддерживается"; exit 1 ;;
  esac
  local ver; ver="$(curl -fsSL 'https://api.github.com/repos/SagerNet/sing-box/releases/latest' | sed -n 's/.*\"tag_name\":[[:space:]]*\"v\([^\"]*\)\".*/\1/p' | head -1 || true)"
  [[ -z "$ver" ]] && ver="$SB_FALLBACK_VER"
  local url="https://github.com/SagerNet/sing-box/releases/download/v${ver}/sing-box-${ver}-linux-${arch}.tar.gz"
  local tmp; tmp="$(mktemp -d)"
  curl -fsSL "$url" -o "$tmp/sb.tar.gz"; tar -xzf "$tmp/sb.tar.gz" -C "$tmp"
  install -m 0755 "$tmp/sing-box-${ver}-linux-${arch}/sing-box" /usr/local/bin/sing-box; rm -rf "$tmp"
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
  msg "venv + зависимости"
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"
}
write_env() {
  msg "пишу $ENV_DIR/env"
  install -d -m 0700 "$ENV_DIR"; umask 077
  {
    echo "WORKERS=$WORKERS"; echo "DEFAULT_LIMIT=$DEFAULT_LIMIT"; echo "SOCKS_PORT=$SOCKS_PORT"
    echo "SUB_HWID=$SUB_HWID"; echo "SUB_UA=$SUB_UA"; echo "DATA_DIR=$DATA_DIR"
    echo "REPO_BRANCH=$REPO_BRANCH"; echo "SINGBOX_BIN=/usr/local/bin/sing-box"
    if [[ "$ROLE" == "agent" ]]; then echo "AGENT_TOKEN=$AGENT_TOKEN"; echo "AGENT_PORT=$AGENT_PORT"
    else echo "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN"; fi
  } > "$ENV_DIR/env"
  chmod 0600 "$ENV_DIR/env"
}
write_service() {
  msg "systemd unit ($SERVICE_NAME)"
  local execmod="bot.tg"; [[ "$ROLE" == "agent" ]] && execmod="bot.agent"
  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=VPN traffic burner ($ROLE)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=$ENV_DIR/env
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m $execmod
StateDirectory=vpn-traffic-bot
Restart=always
RestartSec=5
LimitNOFILE=1048576
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now "${SERVICE_NAME}.service"
}
open_firewall() {
  command -v ufw >/dev/null 2>&1 && ufw allow "${AGENT_PORT}/tcp" >/dev/null 2>&1 || true
  if command -v firewall-cmd >/dev/null 2>&1; then
    firewall-cmd --add-port="${AGENT_PORT}/tcp" --permanent >/dev/null 2>&1 || true
    firewall-cmd --reload >/dev/null 2>&1 || true
  fi
}

install_deps
install_singbox
fetch_repo
setup_venv
write_env
write_service
[[ "$ROLE" == "agent" ]] && open_firewall

sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  msg "сервис запущен: $SERVICE_NAME"
  if [[ "$ROLE" == "agent" ]]; then
    IP="$(curl -fsSL --max-time 5 ifconfig.me 2>/dev/null || echo '<IP>')"
    msg "АГЕНТ ГОТОВ: http://$IP:$AGENT_PORT  token=$AGENT_TOKEN"
  else
    msg "лог: journalctl -u ${SERVICE_NAME} -f"
  fi
else
  err "сервис не поднялся: journalctl -u ${SERVICE_NAME} -n 50 --no-pager"; exit 1
fi
