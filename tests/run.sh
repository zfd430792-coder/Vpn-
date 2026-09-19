#!/usr/bin/env bash
# Прогон всех проверок: bash tests/run.sh
set -u
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
fail=0
for t in tests/test_*.py; do
  printf '\n\033[1;36m═══ %s\033[0m\n' "$t"
  "$PY" "$t" || fail=1
done
echo
if [[ $fail -eq 0 ]]; then
  printf '\033[1;32mВсё зелёное\033[0m\n'
else
  printf '\033[1;31mЕсть провалы\033[0m\n'; exit 1
fi
