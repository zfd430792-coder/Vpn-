# vpn-traffic-bot

Бот, который "съедает" трафик подписки VPN. Забирает подписку по ссылке,
парсит ноды (base64 со списком URI, Clash YAML или готовый sing-box JSON),
поднимает локальный `sing-box` с mixed inbound на `127.0.0.1:10808`
и запускает пачку асинхронных воркеров, качающих большие файлы через
SOCKS в `/dev/null`, пока не будет достигнут лимит трафика.

## Что понимает

Подписка:
- строка base64 со списком `vmess://`, `vless://`, `trojan://`, `ss://`, `hy2://`
- Clash / mihomo YAML (ключ `proxies:`, типы `ss/vmess/vless/trojan/hysteria2`)
- сырой sing-box JSON с ключом `outbounds`

Протоколы outbound'ов, генерируемых для sing-box: VMess (+WS/gRPC/TLS),
VLESS (+WS/gRPC/TLS/Reality/uTLS), Trojan (+WS/TLS), Shadowsocks, Hysteria2.

## Требования

- Python 3.10+
- Установленный `sing-box` в PATH (или путь через `--singbox`).
  Как поставить: https://sing-box.sagernet.org/installation/

## Установка

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

## Запуск

    .venv/bin/python -m bot --sub 'https://sub.owelwe.live/XXXXXXXX'

Полный список флагов:

    --sub URL              URL подписки
    --sub-file PATH        локальный файл вместо URL (для тестов)
    --singbox PATH         путь к бинарю sing-box (по умолчанию из PATH)
    --port N               локальный SOCKS/HTTP порт (10808)
    --workers N            параллельные загрузки (16)
    --limit VALUE          лимит трафика: 100GB, 500MB, 0 = без лимита
    --interval SEC         период строк прогресса (5)
    --files PATH           файл со списком URL для качания (по строке)
    --ua STRING            User-Agent при запросе подписки (v2rayN/6.42)
    --log-level LEVEL      уровень sing-box (warn)
    --dry-run              распарсить и вывести outbounds, не запуская

## Что качает

По умолчанию — большие тестовые файлы Cloudflare / OVH / Tele2 /
LeaseWeb / Thinkbroadband / Cachefly. Полный список в
`bot/traffic.py`. Свой список URL — через `--files`, по строке на URL.

## Примеры

Скушать 100 ГБ и остановиться:

    .venv/bin/python -m bot --sub 'https://sub.owelwe.live/XXX' --limit 100GB

Больше потоков:

    .venv/bin/python -m bot --sub 'https://...' --workers 64 --limit 200GB

Свои файлы:

    .venv/bin/python -m bot --sub 'https://...' --files my-urls.txt

Оффлайн проверка парсинга на локальной подписке:

    .venv/bin/python -m bot --sub-file fixtures/sub-b64.txt --dry-run
    .venv/bin/python -m bot --sub-file fixtures/sub-clash.yaml --dry-run

## Как это работает

1. `bot/subscription.py` — GET на URL с UA `v2rayN/6.42`, попытка
   base64-декода, разбор в один из трёх форматов.
2. `bot/outbound.py` — каждая нода превращается в объект
   sing-box outbound.
3. `bot/singbox.py` — генерирует конфиг с mixed inbound и селектором,
   запускает `sing-box run -c config.json`, ждёт локальный порт.
4. `bot/traffic.py` — N `asyncio`-воркеров через `aiohttp_socks`
   стримят большие файлы через локальный SOCKS в `/dev/null`,
   аккумулируя счётчик байт.
5. `bot/report.py` — каждые `--interval` секунд печатает съеденный
   объём, текущую и среднюю скорость.

## Замечания

- Скорость упирается в скорость самой ноды подписки, поэтому имеет
  смысл гонять с прокси, у которой есть заведомо толстый аплинк.
- Некоторые CDN отдают файлы только с определённых регионов —
  если счётчик замер, добавьте больше зеркал в `--files`.
- Sing-box открывает Clash API на `127.0.0.1:9090`, при желании
  можно из него переключать активный outbound.
