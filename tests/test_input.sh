#!/usr/bin/env bash
# Проверка разбора ввода из install.sh: вставка приносит мусор, и он не должен
# доезжать до конфига. Функции берём из самого установщика, без его запуска.
cd "$(dirname "$0")/.." || exit 1
# shellcheck source=/dev/null
source <(sed -n '1,/^banner$/p' install.sh | head -n -1)
set +e

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32m✅\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m❌\033[0m %s — получили: %q\n' "$1" "$2"; }
eq()   { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "$2"; }
yes_() { $2 "$3" && ok "$1" || bad "$1" "не прошло проверку: $3"; }
no_()  { $2 "$3" && bad "$1" "мусор прошёл: $3" || ok "$1"; }

TOKEN='123456789:AAEhBOweik6ad9r_QXqLhMQNwWFTLLmPL2s'

echo
echo "[1] Очистка вставленного текста"
eq "маркеры bracketed-paste срезаются" \
   "$(sanitize $'\e[200~'"$TOKEN"$'\e[201~')" "$TOKEN"
eq "перевод строки из Windows (\\r) срезается" "$(sanitize "$TOKEN"$'\r')" "$TOKEN"
eq "пробелы по краям срезаются"     "$(sanitize "   $TOKEN   ")" "$TOKEN"
eq "табы срезаются"                 "$(sanitize $'\t'"$TOKEN"$'\t')" "$TOKEN"
eq "двойные кавычки срезаются"      "$(sanitize "\"$TOKEN\"")" "$TOKEN"
eq "одинарные кавычки срезаются"    "$(sanitize "'$TOKEN'")" "$TOKEN"
eq "управляющие символы срезаются"  "$(sanitize $'\x01'"$TOKEN"$'\x7f')" "$TOKEN"
eq "чистое значение не портится"    "$(sanitize "$TOKEN")" "$TOKEN"

echo
echo "[2] Выдёргивание токена из текста вокруг"
eq "токен из фразы BotFather" \
   "$(extract_token "Use this token to access the HTTP API:
$TOKEN
Keep your token secure")" "$TOKEN"
eq "токен с префиксом 'token:'"    "$(extract_token "token: $TOKEN")" "$TOKEN"
eq "токен внутри строки"           "$(extract_token "вот он $TOKEN держи")" "$TOKEN"
eq "чистый токен как есть"         "$(extract_token "$TOKEN")" "$TOKEN"
eq "из мусора без токена — пусто"  "$(extract_token "просто болтовня")" ""

echo
echo "[3] Проверка формата токена"
yes_ "настоящий токен принимается"        valid_token "$TOKEN"
no_  "@имя бота отклоняется"              valid_token "@my_voice_bot"
no_  "токен без двоеточия отклоняется"    valid_token "123456789AAEhBOweik6ad9r"
no_  "обрезанный токен отклоняется"       valid_token "123456789:AAEh"
no_  "пустое отклоняется"                 valid_token ""
no_  "токен с маркером вставки отклоняется" valid_token $'\e[200~'"$TOKEN"

echo
echo "[4] Токен из окружения — без вопросов"
BOT_TOKEN=$'\e[200~ "token: '"$TOKEN"$'"\r\e[201~'
ask_token >/dev/null
eq "грязный BOT_TOKEN из окружения вычищен" "$BOT_TOKEN" "$TOKEN"

echo
echo "[5] Маскирование при показе"
eq "длинное значение маскируется" "$(mask "$TOKEN")" "123456789:…PL2s"
eq "короткое значение маскируется" "$(mask "12345")" "123***"

echo
echo "───────────────────────────────────────"
if (( FAIL )); then
  printf '\033[31m❌ провалено: %d, прошло: %d\033[0m\n' "$FAIL" "$PASS"; exit 1
fi
printf '\033[32m✅ разбор ввода: все %d проверки прошли\033[0m\n' "$PASS"
