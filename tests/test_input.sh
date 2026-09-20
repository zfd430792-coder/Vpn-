#!/usr/bin/env bash
# Проверка разбора ввода из install.sh: вставка приносит мусор, и он не должен
# доезжать до конфига. Функции берём из самого установщика, без его запуска.
cd "$(dirname "$0")/.."
source <(sed -n '1,/^banner$/p' install.sh | head -n -1)
set +e

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); printf '  \033[32m✅\033[0m %s\n' "$1"; }
bad()  { FAIL=$((FAIL+1)); printf '  \033[31m❌\033[0m %s — получили: %q\n' "$1" "$2"; }
eq()   { [[ "$2" == "$3" ]] && ok "$1" || bad "$1" "$2"; }
yes_() { $2 "$3" && ok "$1" || bad "$1" "не прошло проверку: $3"; }
no_()  { $2 "$3" && bad "$1" "мусор прошёл: $3" || ok "$1"; }

TOKEN='123456789:AAEhBOweik6ad9r_QXqLhMQNwWFTLLmPL2s'
HASH='0123456789abcdef0123456789abcdef'

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
no_  "@имя бота отклоняется"              valid_token "@my_anime_bot"
no_  "токен без двоеточия отклоняется"    valid_token "123456789AAEhBOweik6ad9r"
no_  "обрезанный токен отклоняется"       valid_token "123456789:AAEh"
no_  "пустое отклоняется"                 valid_token ""
no_  "токен с маркером вставки отклоняется" valid_token $'\e[200~'"$TOKEN"

echo
echo "[4] Telegram ID"
eq "ID из '@userinfobot: Id: 123456789'" "$(extract_digits "Id: 123456789")" "123456789"
eq "ID с пробелами"                      "$(extract_digits "  987654321 ")" "987654321"
yes_ "нормальный ID принимается"         valid_id "123456789"
no_  "@имя отклоняется"                  valid_id "@vasya"
no_  "слишком короткий отклоняется"      valid_id "12"

echo
echo "[5] API_ID и API_HASH"
yes_ "API_ID принимается"                valid_apiid "1234567"
no_  "API_ID из букв отклоняется"        valid_apiid "abcdefg"
eq   "API_HASH из текста"                "$(extract_hash "api_hash: $HASH")" "$HASH"
eq   "API_HASH в верхнем регистре"       "$(extract_hash "${HASH^^}")" "${HASH^^}"
yes_ "API_HASH принимается"              valid_hash "$HASH"
yes_ "API_HASH в верхнем регистре принимается" valid_hash "${HASH^^}"
no_  "короткий API_HASH отклоняется"     valid_hash "0123456789abcdef"
no_  "API_HASH с буквой z отклоняется"   valid_hash "z123456789abcdef0123456789abcdef"

echo
echo "[6] Маскирование при показе"
eq "длинное значение маскируется" "$(mask "$TOKEN")" "123456789:…PL2s"
eq "короткое значение маскируется" "$(mask "12345")" "123***"

echo
echo "───────────────────────────────────────"
if (( FAIL )); then
  printf '\033[31m❌ провалено: %d, прошло: %d\033[0m\n' "$FAIL" "$PASS"; exit 1
fi
printf '\033[32m✅ разбор ввода: все %d проверки прошли\033[0m\n' "$PASS"
