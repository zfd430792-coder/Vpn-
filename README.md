# vpn-traffic-bot

Бот, который "съедает" трафик подписки VPN. Забирает подписку по ссылке,
парсит ноды (base64 со списком URI, Clash YAML или готовый sing-box JSON),
поднимает локальный `sing-box` с mixed inbound на `127.0.0.1:10808`
и запускает пачку асинхронных воркеров, качающих большие файлы через
SOCKS в `/dev/null`, пока не будет достигнут лимит.

Два режима работы:

- **CLI** — прямой запуск: подаёшь URL подписки флагом, смотришь прогресс в терминале.
- **Telegram-бот** — сидит демоном; кидаешь в лички боту URL подписки, он жрёт; команды `/status`, `/stop`, `/limit`.

## Одна команда на сервер (Telegram-бот + systemd)

Ubuntu/Debian, CentOS/RHEL, Alpine, под root. Токен передаётся env-переменной:

    curl -fsSL https://raw.githubusercontent.com/zfd430792-coder/Vpn-/claude/traffic-consuming-bot-iuxyrf/install.sh \
      | sudo TELEGRAM_BOT_TOKEN='ТУТ_ТВОЙ_ТОКЕН' bash

Что делает `install.sh`:

1. ставит `python3`/`git`/`curl`/`tar` под текущий пакетный менеджер;
2. качает статический бинарник `sing-box` с GitHub releases в `/usr/local/bin`;
3. клонирует репо в `/opt/vpn-traffic-bot`, делает venv, ставит зависимости;
4. пишет `/etc/vpn-traffic-bot/env` (0600) с токеном и параметрами;
5. создаёт systemd-юнит `vpn-traffic-bot.service`, включает и запускает.

После установки:

    journalctl -u vpn-traffic-bot -f     # лог
    systemctl status vpn-traffic-bot     # статус
    systemctl restart vpn-traffic-bot    # рестарт

Переменные (можно перекрыть перед запуском `install.sh`):

- `WORKERS` — параллельных загрузок (32)
- `DEFAULT_LIMIT` — лимит по умолчанию, `100GB`/`500MB`/`0` без лимита
- `SOCKS_PORT` — локальный порт (10808)
- `TELEGRAM_ALLOWED_CHATS` — CSV chat_id, кому можно писать боту (пусто = всем)

## Работа с ботом в Telegram

Всё управление — кнопками в одном самообновляемом сообщении.
Любая команда (кроме `/stop` и `/update`) просто открывает меню.

Главное меню:

    ▶️ Запустить всё        (во время жора — ⏹ Остановить всё)
    🔑 Ключи      🖥 Серверы
    📊 Статус     🎯 Источники
    🩺 Диагностика ⚙️ Настройки

- **Ключи** — список подписок, по кнопке на каждую. Нажатие открывает
  карточку ключа: запуск, проверка, выбор страны, HWID, удаление.
- **Серверы** — доп. машины с агентом. Карточка сервера: пинг,
  назначение ключа, удаление.
- **Источники** — что качать. Можно добавить свои URL и переключиться
  в режим «только свои» (нужно для «умных» VPN).
- **Диагностика** — маршрут (реально ли трафик идёт через ноды),
  инфо о хостере и платности трафика, скорость канала.
- **Настройки** — воркеры, лимит запуска, самообновление с GitHub.

Ещё быстрее: просто кинь боту ссылку на подписку — заведёт ключ и
сразу запустит. Или пришли сами ссылки `vless://` из Happ — заведётся
«ручной ключ» без панели и HWID.

Команды: `/stop` — остановить всё, `/update` — обновиться с GitHub.

## Ручной запуск CLI

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python -m bot --sub 'https://sub.owelwe.live/XXXXXXXX' --limit 100GB

Флаги CLI:

    --sub URL              URL подписки
    --sub-file PATH        локальный файл вместо URL (для тестов)
    --singbox PATH         путь к бинарю sing-box
    --port N               локальный SOCKS/HTTP порт (10808)
    --workers N            параллельные загрузки (16)
    --limit VALUE          100GB, 500MB, 0 = без лимита
    --interval SEC         период строк прогресса (5)
    --files PATH           файл со списком URL для качания
    --ua STRING            User-Agent при запросе подписки
    --log-level LEVEL      уровень sing-box (warn)
    --dry-run              распарсить и вывести outbounds, не запуская

## Форматы подписки

- base64 со списком `vmess://`, `vless://`, `trojan://`, `ss://`, `hy2://`
- Clash / mihomo YAML (ключ `proxies:`)
- сырой sing-box JSON с ключом `outbounds`

Оффлайн проверка парсинга:

    .venv/bin/python -m bot --sub-file fixtures/sub-b64.txt --dry-run
    .venv/bin/python -m bot --sub-file fixtures/sub-clash.yaml --dry-run

## Что качает

По умолчанию — большие тестовые файлы Cloudflare / OVH / Tele2 /
LeaseWeb / Thinkbroadband / Cachefly. Свой список URL — через `--files`,
по строке на URL.

## Устройство

- `bot/subscription.py` — GET, base64-декод, разбор в один из трёх форматов
- `bot/outbound.py` — конвертация нод в объекты sing-box outbound
- `bot/singbox.py` — генерация конфига, запуск процесса, ожидание порта
- `bot/traffic.py` — N `asyncio`-воркеров через `aiohttp_socks` в `/dev/null`
- `bot/report.py` — прогресс и парсинг размеров `100GB`
- `bot/__main__.py` — CLI
- `bot/tg.py` — Telegram-бот на long-polling
- `install.sh` — one-shot установщик под systemd

## Замечания

- Скорость упирается в аплинк ноды подписки.
- Если счётчик замер — некоторые CDN отдают файлы только из определённых
  регионов; добавь свои через `--files`.
- Sing-box открывает Clash API на `127.0.0.1:9090` — можно из него
  переключать активный outbound руками.
- **Токен бота хранится только в `/etc/vpn-traffic-bot/env` (0600) и в
  env-переменной systemd**. В репо ничего не пишется. Если токен где-то
  засветился — сделай в `@BotFather` `/revoke` и перезапиши файл.
