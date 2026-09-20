#!/usr/bin/env bash
# Интерактивный опрос install.sh в настоящем псевдотерминале:
# мусор должен отсекаться и переспрашиваться, а вставка — доезжать чистой.
cd "$(dirname "$0")/.."
if ! command -v script >/dev/null 2>&1; then
  printf '  \033[33m⚠️  пропущено: нет утилиты script (нужен псевдотерминал)\033[0m\n'
  exit 0
fi

TOKEN='123456789:AAEhBOweik6ad9r_QXqLhMQNwWFTLLmPL2s'
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/drive.sh" <<'INNER'
cd "$REPO"
source <(sed -n '1,/^banner$/p' install.sh | head -n -1)
set +e
check_token() { BOT_USERNAME="test_bot"; [[ "$1" == *":"* ]]; }
BOT_TOKEN=""; ADMINS=""
ask_value BOT_TOKEN "Токен" extract_token valid_token "плохой токен"
ask_value ADMINS    "ID"    extract_digits valid_id  "нужен номер"
echo "OUT_TOKEN=[$BOT_TOKEN]"
echo "OUT_ID=[$ADMINS]"
INNER

# сценарий ввода: мусор → @имя → токен в маркерах вставки → @ник → строка от @userinfobot
printf 'не токен\n@my_bot\n\033[200~%s\033[201~\n@vasya\nId: 123456789\n' "$TOKEN" > "$WORK/in"

OUT="$(REPO="$PWD" script -qec "bash $WORK/drive.sh" /dev/null < "$WORK/in" 2>&1 | tr -d '\r')"

PASS=0; FAIL=0
check() {
  if [[ "$2" == *"$3"* ]]; then PASS=$((PASS+1)); printf '  \033[32m✅\033[0m %s\n' "$1"
  else FAIL=$((FAIL+1)); printf '  \033[31m❌\033[0m %s\n' "$1"; fi
}
echo
check "токен доехал чистым, без маркеров вставки" "$OUT" "OUT_TOKEN=[$TOKEN]"
check "ID выдернут из строки @userinfobot"        "$OUT" "OUT_ID=[123456789]"
check "на мусор переспросил, а не упал"           "$OUT" "плохой токен"
check "на @имя вместо ID переспросил"             "$OUT" "нужен номер"

echo
if (( FAIL )); then
  printf '\033[31m❌ провалено: %d\033[0m\n' "$FAIL"; exit 1
fi
printf '\033[32m✅ интерактивный опрос: все %d проверки прошли\033[0m\n' "$PASS"
