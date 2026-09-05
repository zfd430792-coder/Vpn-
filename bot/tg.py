import asyncio
import os
import re
import secrets
import socket
import time
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import aiohttp

from .engine import BurnSession
from .hoster import summarize as hoster_summary
from .loader import HAPP_UA, fetch_and_load, is_placeholder, outbounds_from_body
from .provision import provision_agent
from .report import fmt_bytes, plan_summary, units_to_bytes
from .selfupdate import local_head, remote_head, run_self_update
from .store import KeyStore, default_name
from .traffic import BIG_FILES


API = "https://api.telegram.org"

WORKER_PRESETS = [32, 64, 128, 256]
# (token, bytes, подпись на кнопке) — подпись самоописательна, заголовок ряду не нужен
LIMIT_PRESETS = [
    ("auto", 0, "∞ Без лимита"),
    ("100g", 100 * 1024**3, "100 GB"),
    ("500g", 500 * 1024**3, "500 GB"),
    ("1t", 1024**4, "1 TB"),
]


def esc(v) -> str:
    """Экранирование для parse_mode=HTML. Всё, что пришло извне (имена
    ключей, теги нод, тексты ошибок), прогоняется через него."""
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_html(text: str) -> str:
    """Откат к plain-тексту, если Telegram не принял разметку."""
    out = re.sub(r"<[^>]+>", "", text)
    return out.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _short(s, n: int) -> str:
    """Обрезка под ширину кнопки/строки."""
    s = str(s or "").strip()
    return s if len(s) <= n else s[: max(n - 1, 1)] + "…"


_STATUS_ICON = {"жив": "✅", "мёртв/исчерпан": "⛔", "заглушки/HWID": "🔒"}


def _key_icon(key: dict) -> str:
    return _STATUS_ICON.get((key.get("report") or {}).get("status", ""), "▫️")


def _host_of(url: str) -> str:
    try:
        return urlparse(url).hostname or url
    except Exception:
        return url


def _extract_url(text: str) -> Optional[str]:
    for token in text.split():
        low = token.lower()
        if low.startswith("http://") or low.startswith("https://"):
            return token
    return None


def _ago(ts: Optional[int]) -> str:
    if not ts:
        return ""
    d = int(time.time()) - int(ts)
    if d < 60:
        return f"{d}с назад"
    if d < 3600:
        return f"{d // 60}м назад"
    if d < 86400:
        return f"{d // 3600}ч назад"
    return f"{d // 86400}д назад"


def _dur(sec: float) -> str:
    sec = int(sec)
    if sec < 60:
        return f"{sec}с"
    m, s = divmod(sec, 60)
    if m < 60:
        return f"{m}м {s:02d}с"
    h, m = divmod(m, 60)
    return f"{h}ч {m:02d}м"


def _sz(v: float) -> str:
    return fmt_bytes(v).strip()


def _country_of(tag: str) -> str:
    t = str(tag or "").strip()
    for sep in ("|", "—", "-", "·"):
        if sep in t:
            t = t.split(sep)[-1].strip()
            break
    return t or "нода"


def _countries(outbounds: List[dict]) -> List[str]:
    seen: List[str] = []
    for o in outbounds:
        c = _country_of(o.get("tag"))
        if c not in seen:
            seen.append(c)
    return seen


def _filter_geo(outbounds: List[dict], sel: Optional[str]) -> List[dict]:
    if not sel:
        return outbounds
    low = sel.lower()
    f = [o for o in outbounds if _country_of(o.get("tag")) == sel or low in str(o.get("tag", "")).lower()]
    return f or outbounds


def _is_placeholder(tag: str) -> bool:
    return is_placeholder(tag)


def _all_placeholders(outbounds: List[dict]) -> bool:
    return bool(outbounds) and all(_is_placeholder(o.get("tag")) for o in outbounds)


def _rand_hwid() -> str:
    """Настоящий Happ шлёт HWID в виде UUID — повторяем формат."""
    b = bytearray(secrets.token_bytes(16))
    b[6] = (b[6] & 0x0F) | 0x40   # версия 4
    b[8] = (b[8] & 0x3F) | 0x80   # вариант RFC 4122
    h = b.hex()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


_NODE_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://", "hy2://", "hysteria2://", "tuic://")


def _looks_like_config(text: str) -> bool:
    low = (text or "").lower()
    return any(s in low for s in _NODE_SCHEMES)


def _is_http(url: str) -> bool:
    return (url or "").lower().startswith(("http://", "https://"))


# хостеры, где ИСХОДЯЩИЙ трафик платный (оплата за ГБ сверх квоты)
_METERED_HOSTS = (
    "amazon", "aws", "ec2", "google", "gcp", "1e100", "microsoft", "azure",
    "digitalocean", "vultr", "choopa", "the constant company", "linode", "akamai",
    "oracle", "alibaba", "aliyun", "tencent", "huawei",
)
# хостеры с фиксом/безлимитом (или очень высокой квотой)
_FLAT_HOSTS = (
    "hetzner", "ovh", "contabo", "netcup", "leaseweb", "time4vps", "aeza", "vdsina",
    "selectel", "firstvds", "firstbyte", "reg.ru", "regru", "timeweb", "fdcservers",
    "servers.com", "melbicom", "melbikomas", "ihor", "hostkey", "pq hosting", "pqhosting",
    "vdsina", "aeza", "cloudzy", "rackm", "datacamp",
)


def _traffic_verdict(blob: str):
    b = (blob or "").lower()
    if any(m in b for m in _METERED_HOSTS):
        return ("⛔ ПЛАТНЫЙ трафик (оплата за ГБ)!",
                "жор терабайтов = БОЛЬШОЙ счёт.\nпроверь тариф ПЕРЕД запуском.")
    if any(f in b for f in _FLAT_HOSTS):
        return ("✅ обычно фикс/безлимит",
                "но глянь лимит (напр. Hetzner режет скорость после ~20 ТБ).")
    return ("❓ хостер неизвестен",
            "УТОЧНИ у хостера, платный ли исходящий трафик, ДО жора.")


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _kb(rows: List[List[dict]]) -> dict:
    return {"inline_keyboard": rows}


BACK = [[_btn("⬅️ Меню", "menu")]]
SRV_BACK = [[_btn("⬅️ Серверы", "servers")]]


def _parse_ssh(text: str):
    parts = text.replace(":", " ").split()
    if len(parts) < 2:
        return None
    host = parts[0]
    rest = parts[1:]
    port = 22
    if len(rest) >= 2 and rest[-1].isdigit() and 1 <= int(rest[-1]) <= 65535:
        port = int(rest[-1])
        rest = rest[:-1]
    if len(rest) >= 2:
        user, password = rest[0], rest[1]
    elif len(rest) == 1:
        user, password = "root", rest[0]
    else:
        return None
    return host, port, user, password


class TgClient:
    def __init__(self, session: aiohttp.ClientSession, token: str):
        self.session = session
        self.token = token

    async def call(self, method: str, **params) -> dict:
        url = f"{API}/bot{self.token}/{method}"
        try:
            async with self.session.post(url, json=params) as r:
                return await r.json()
        except Exception:
            return {}

    async def _text_call(self, method: str, text: str, **params) -> dict:
        """Шлём с HTML-разметкой; если Telegram её не принял (незакрытый тег,
        неэкранированный «<» в имени ключа), повторяем без разметки — экран
        придёт простым текстом, но НЕ потеряется."""
        params.update({"text": text, "disable_web_page_preview": True, "parse_mode": "HTML"})
        r = await self.call(method, **params)
        if isinstance(r, dict) and r.get("ok"):
            return r
        params.pop("parse_mode", None)
        params["text"] = strip_html(text)
        return await self.call(method, **params)

    async def send(self, chat_id: int, text: str, kb: Optional[dict] = None) -> dict:
        params = {"chat_id": chat_id}
        if kb:
            params["reply_markup"] = kb
        return await self._text_call("sendMessage", text, **params)

    async def edit(self, chat_id: int, mid: int, text: str, kb: Optional[dict] = None) -> None:
        params = {"chat_id": chat_id, "message_id": mid}
        if kb:
            params["reply_markup"] = kb
        await self._text_call("editMessageText", text, **params)

    async def answer(self, cb_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=cb_id, text=text)


class Bot:
    def __init__(self, tg: TgClient, store: KeyStore, cfg: dict):
        self.tg = tg
        self.store = store
        self.cfg = cfg
        self.session = BurnSession(cfg["workers"], cfg["singbox_bin"], cfg["port"])
        self.busy = False
        self._stop_all = False
        self.pending_limit = cfg["default_limit"]
        self.awaiting: Dict[int, str] = {}
        self.chats: Set[int] = set(int(c) for c in (store.settings.get("chats") or []))
        self._notified_head = ""
        self._geo_cache: Dict[int, List[str]] = {}

    def _remember(self, chat_id: int) -> None:
        if chat_id not in self.chats:
            self.chats.add(chat_id)
            self.store.set_setting("chats", list(self.chats))

    def _workers(self) -> int:
        w = self.store.settings.get("workers")
        return int(w) if w else self.cfg["workers"]

    def _files(self) -> List[str]:
        if self.store.settings.get("custom_only") and self.store.targets:
            return list(self.store.targets)
        return list(BIG_FILES) + list(self.store.targets)

    def _take_limit(self) -> int:
        lim = self.pending_limit
        self.pending_limit = self.cfg["default_limit"]
        return lim

    async def _agent_call(self, agent: dict, method: str, path: str, payload: Optional[dict] = None):
        base = (agent.get("url") or "").rstrip("/")
        if not base:
            return None
        headers = {"X-Token": agent.get("token", "")}
        try:
            if method == "GET":
                async with self.tg.session.get(base + path, headers=headers,
                                               timeout=aiohttp.ClientTimeout(total=10)) as r:
                    return await r.json()
            async with self.tg.session.post(base + path, headers=headers, json=payload or {},
                                            timeout=aiohttp.ClientTimeout(total=15)) as r:
                return await r.json()
        except Exception:
            return None

    async def _key_load(self, key: dict):
        try:
            ob, raw, info, ua = await self._fetch_nodes(key, tries=2, delay=3.0)
        except Exception:
            return [], 0, {}
        ob = _filter_geo(ob, key.get("country"))
        _, _, rem = plan_summary(info)
        return ob, rem, info

    async def _fetch_nodes(self, key: dict, tries: int = 3, delay: float = 3.0, on_try=None):
        """Тянет ноды. Если ключ — не URL, а сами ссылки (vless://…), парсим
        их напрямую (без сети, без HWID — обход панели-стены).
        Иначе тянем подписку: панель на первый запрос с новым HWID часто
        регистрирует устройство и отдаёт заглушки, а реальный конфиг — на
        следующий; поэтому повторяем тем же HWID (новый НЕ генерим)."""
        loop = asyncio.get_event_loop()
        src = key.get("url", "")
        if not _is_http(src):
            try:
                ob = await loop.run_in_executor(None, lambda: outbounds_from_body(src))
            except Exception:
                ob = []
            real = [o for o in ob if not _is_placeholder(o.get("tag"))]
            return real, real, {}, "manual"
        hwid = (key.get("hwid") or self.cfg["hwid"]) or None
        raw_last: List[dict] = []
        info_last: Dict[str, int] = {}
        ua_last = ""
        for t in range(max(tries, 1)):
            ob, ua, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"], hwid=hwid))
            ua_last = ua
            raw_last = raw or ob
            info_last = info
            real = [o for o in ob if not _is_placeholder(o.get("tag"))]
            if real:
                return real, raw_last, info_last, ua_last
            if t < tries - 1:
                if on_try:
                    await on_try(t + 1, tries)
                await asyncio.sleep(delay)
        return [], raw_last, info_last, ua_last

    # ---------- views ----------
    # Правила экранов, чтобы не расползалось:
    #   • заголовок — <b>жирный</b>, один на экран, без ВЕРХНЕГО РЕГИСТРА;
    #   • пары «Поле — значение» через тире: пробелами в Telegram не выровнять,
    #     шрифт пропорциональный, попытка выравнивания и даёт кашу;
    #   • разделитель — пустая строка, а не полоса ━━━ в каждом блоке;
    #   • список = по одной кнопке на элемент (имя, а не голая цифра),
    #     действия живут в карточке элемента;
    #   • последний ряд — навигация «назад».

    def _menu_text(self) -> str:
        s = self.session
        if s.running():
            eaten = s.counter.bytes if s.counter else 0
            state = f"🔥 <b>Жру трафик</b> · {esc(_sz(eaten))}"
        else:
            state = "💤 <b>Простой</b>"
        return (
            "🚀 <b>VPN Traffic Bot</b>\n"
            f"{state}\n\n"
            f"🔑 Ключей — {len(self.store.keys)}\n"
            f"🖥 Серверов — {len(self.store.servers)}\n"
            f"🧵 Воркеров — {self._workers()}\n"
            f"🎯 Источников — {len(self._files())}"
        )

    def _menu_kb(self) -> dict:
        # Первая кнопка меняется по состоянию: «Остановить» на простое —
        # мёртвая кнопка, «Запустить» во время жора — двойной запуск.
        top = (_btn("⏹ Остановить всё", "stop") if self.session.running()
               else _btn("▶️ Запустить всё", "run_all"))
        return _kb([
            [top],
            [_btn("🔑 Ключи", "keys"), _btn("🖥 Серверы", "servers")],
            [_btn("📊 Статус", "status"), _btn("🎯 Источники", "targets")],
            [_btn("🩺 Диагностика", "diag"), _btn("⚙️ Настройки", "settings")],
        ])

    # ---- ключи ----
    def _keys_text(self) -> str:
        if not self.store.keys:
            return ("🔑 <b>Ключи</b>\n\n"
                    "Пока пусто.\n\n"
                    "Нажми «➕ Добавить» или просто пришли ссылку "
                    "на подписку в чат — заведу и запущу.")
        out = ["🔑 <b>Ключи</b>", ""]
        for i, k in enumerate(self.store.keys, 1):
            r = k.get("report") or {}
            out.append(f"{_key_icon(k)} <b>{i}.</b> {esc(_short(k.get('name', 'key'), 32))}")
            bits = []
            if r.get("eaten"):
                bits.append(f"съел {_sz(r['eaten'])}")
            if r.get("total"):
                bits.append(f"план {_sz(r['used'])} / {_sz(r['total'])}")
            if r.get("ts"):
                bits.append(_ago(r["ts"]))
            out.append("      " + esc(" · ".join(bits)) if bits else "      ещё не запускался")
        out += ["", "Нажми на ключ — откроется карточка."]
        return "\n".join(out)

    def _keys_kb(self) -> dict:
        rows = [[_btn(f"{_key_icon(k)} {i + 1}. {_short(k.get('name', 'key'), 26)}", f"key:{i}")]
                for i, k in enumerate(self.store.keys)]
        if self.store.keys:
            rows.append([_btn("▶️ Запустить все", "run_all")])
        rows.append([_btn("➕ Добавить", "addkey"), _btn("⬅️ Меню", "menu")])
        return _kb(rows)

    def _key_text(self, i: int) -> str:
        k = self.store.get(i)
        if not k:
            return "🔑 <b>Ключ</b>\n\nЭтого ключа больше нет."
        r = k.get("report") or {}
        lines = [f"🔑 <b>{esc(_short(k.get('name', 'key'), 40))}</b>", "",
                 f"Статус — {_key_icon(k)} {esc(r.get('status') or 'не проверялся')}"]
        if r.get("eaten"):
            lines.append(f"Съедено — {esc(_sz(r['eaten']))}")
        if r.get("total"):
            lines.append(f"План — {esc(_sz(r['used']))} / {esc(_sz(r['total']))}")
        lines.append(f"Страна — {esc(k.get('country') or 'авто (все)')}")
        lines.append(f"HWID — {esc(k.get('hwid') or 'не задан')}")
        if r.get("ts"):
            lines.append(f"Проверен — {esc(_ago(r['ts']))}")
        if r.get("note"):
            lines += ["", f"⚠ {esc(r['note'])}"]
        return "\n".join(lines)

    def _key_kb(self, i: int) -> dict:
        return _kb([
            [_btn("▶️ Запустить", f"run:{i}")],
            [_btn("🔍 Проверить", f"check:{i}"), _btn("🌍 Страна", f"geo:{i}")],
            [_btn("🏢 Хостер", f"host:{i}")],
            [_btn("🆔 HWID", f"hwid:{i}"), _btn("🗑 Удалить", f"delask:{i}")],
            [_btn("⬅️ Ключи", "keys")],
        ])

    # ---- серверы ----
    def _servers_text(self) -> str:
        if not self.store.servers:
            return ("🖥 <b>Серверы</b>\n\n"
                    "Дополнительных серверов нет.\n\n"
                    "«➕ Добавить» → пришлёшь SSH-доступ, бот сам зайдёт, "
                    "поставит агента и подключит.\n"
                    "Несколько машин жрут параллельно — быстрее.")
        out = ["🖥 <b>Серверы</b>", ""]
        for i, s in enumerate(self.store.servers, 1):
            ku = s.get("key_url")
            kn = (self.store.key_by_url(ku) or {}).get("name", "?") if ku else "общий (ключ №1)"
            out.append(f"<b>{i}.</b> {esc(_short(s.get('name', ''), 32))}")
            out.append(f"      🔑 {esc(_short(kn, 30))}")
        out += ["", "Нажми на сервер — откроется карточка."]
        return "\n".join(out)

    def _servers_kb(self) -> dict:
        rows = [[_btn(f"{i + 1}. {_short(s.get('name', ''), 26)}", f"srv:{i}")]
                for i, s in enumerate(self.store.servers)]
        if self.store.servers:
            rows.append([_btn("🧩 Раздать по назначению", "dist_run")])
            rows.append([_btn("🔄 Обновить агентов", "update_agents")])
        rows.append([_btn("➕ Добавить", "srvadd"), _btn("⬅️ Меню", "menu")])
        return _kb(rows)

    def _srv_text(self, i: int) -> str:
        s = self.store.servers[i] if 0 <= i < len(self.store.servers) else None
        if not s:
            return "🖥 <b>Сервер</b>\n\nЭтого сервера больше нет."
        ku = s.get("key_url")
        kn = (self.store.key_by_url(ku) or {}).get("name", "?") if ku else "общий (ключ №1)"
        return (f"🖥 <b>{esc(_short(s.get('name', ''), 40))}</b>\n\n"
                f"Адрес — <code>{esc(s.get('url', ''))}</code>\n"
                f"Жрёт ключ — {esc(_short(kn, 32))}")

    def _srv_kb(self, i: int) -> dict:
        return _kb([
            [_btn("🔌 Проверить связь", f"sping:{i}")],
            [_btn("🔑 Назначить ключ", f"akey:{i}"), _btn("🗑 Удалить", f"sdelask:{i}")],
            [_btn("⬅️ Серверы", "servers")],
        ])

    def _akey_text(self, ai: int) -> str:
        s = self.store.servers[ai] if 0 <= ai < len(self.store.servers) else None
        return (f"🔑 <b>Ключ для сервера</b>\n\n{esc(_short((s or {}).get('name', '?'), 40))}\n\n"
                "Выбери, что он будет жрать:")

    def _akey_kb(self, ai: int) -> dict:
        rows = [[_btn(f"{_key_icon(k)} {_short(k.get('name', f'key{j + 1}'), 28)}", f"setk:{ai}:{j}")]
                for j, k in enumerate(self.store.keys)]
        rows.append([_btn("✖️ Сбросить (общий №1)", f"setk:{ai}:-1")])
        rows.append([_btn("⬅️ Назад", f"srv:{ai}")])
        return _kb(rows)

    # ---- источники ----
    def _targets_text(self) -> str:
        co = bool(self.store.settings.get("custom_only"))
        mode = "только свои" if (co and self.store.targets) else "встроенные + свои"
        out = ["🎯 <b>Источники</b>", "",
               f"Режим — {mode}",
               f"Встроенных — {len(BIG_FILES)}",
               f"Своих — {len(self.store.targets)}"]
        if self.store.targets:
            out.append("")
            for i, t in enumerate(self.store.targets, 1):
                out.append(f"<b>{i}.</b> {esc(_short(t, 46))}")
        return "\n".join(out)

    def _targets_kb(self) -> dict:
        co = bool(self.store.settings.get("custom_only"))
        rows = [[_btn(("✅" if co else "⬜️") + " Только свои источники", "toggle_custom")]]
        rows += [[_btn(f"🗑 {i + 1}. {_short(_host_of(t), 26)}", f"tdel:{i}")]
                 for i, t in enumerate(self.store.targets)]
        rows.append([_btn("➕ Добавить", "addtarget"), _btn("❓ Зачем это", "thelp")])
        rows.append([_btn("⬅️ Меню", "menu")])
        return _kb(rows)

    def _thelp_text(self) -> str:
        return ("❓ <b>Счётчик подписки не растёт?</b>\n\n"
                "Значит у тебя «умный» VPN: он гонит через ноду только "
                "заблокированные сайты, а обычные CDN пропускает мимо — "
                "квота на них не тратится.\n\n"
                "<b>Что делать:</b>\n"
                "1. Добавь сюда прямую ссылку на крупный файл с ресурса, "
                "который без VPN у тебя не открывается (например archive.org).\n"
                "2. Включи «Только свои источники».")

    # ---- диагностика ----
    def _diag_text(self) -> str:
        return ("🩺 <b>Диагностика</b>\n\n"
                "🔎 <b>Маршрут</b> — идёт ли трафик через ноды подписки\n"
                "🖧 <b>Сервер</b> — хостер, IP, платный ли трафик\n"
                "⚡ <b>Скорость</b> — канал самой машины")

    def _diag_kb(self) -> dict:
        return _kb([
            [_btn("🔎 Маршрут", "routechk"), _btn("🖧 Сервер", "server")],
            [_btn("⚡ Скорость канала", "spdtest")],
            [_btn("⬅️ Меню", "menu")],
        ])

    # ---- настройки ----
    def _settings_text(self) -> str:
        lim = self.pending_limit
        ver = local_head(self.cfg["install_dir"])[:7] or "?"
        return ("⚙️ <b>Настройки</b>\n\n"
                f"🧵 Воркеров — {self._workers()}\n"
                f"🎯 Лимит запуска — {esc(_sz(lim) if lim else 'без лимита')}\n"
                f"📟 Версия — <code>{esc(ver)}</code>\n\n"
                "Больше воркеров — больше трафика, пока хватает канала.\n"
                "Обновление тянется с GitHub, заходить по SSH не нужно.")

    def _settings_kb(self) -> dict:
        cur = self._workers()
        wrow = [_btn(("• " if n == cur else "") + f"{n} 🧵", f"wrk:{n}") for n in WORKER_PRESETS]
        lrow = [_btn(("• " if self.pending_limit == v else "") + label, f"lim:{t}")
                for t, v, label in LIMIT_PRESETS]
        return _kb([
            wrow,
            lrow,
            [_btn("🔄 Проверить обновления", "chkupd")],
            [_btn("⬆️ Обновить бота", "update_bot"), _btn("⬆️ Обновить всё", "update_all")],
            [_btn("⬅️ Меню", "menu")],
        ])

    # ---- подтверждение удаления ----
    def _confirm(self, what: str, name: str, yes: str, no: str):
        text = (f"🗑 <b>Удалить {esc(what)}?</b>\n\n"
                f"{esc(_short(name, 60))}\n\nОтменить будет нельзя.")
        return text, _kb([[_btn("✅ Да, удалить", yes), _btn("✖️ Отмена", no)]])

    # ---- статус ----
    def _running_kb(self) -> dict:
        return _kb([[_btn("⏹ Остановить", "stop"), _btn("🔄 Обновить", "status")]])

    def _status_kb(self) -> dict:
        return _kb([[_btn("🔄 Обновить", "status"), _btn("⬅️ Меню", "menu")]])

    def _status_card(self, title: str = "") -> str:
        s = self.session
        if not s.counter:
            return "💤 <b>Простой</b>\n\nДобавь ключ и жми ▶️."
        elapsed = max(time.monotonic() - s.started_at, 1e-6)
        eaten = s.counter.bytes
        name = title or s.title
        head = "🔥 <b>Жру трафик</b>" if s.running() else "⏹ <b>Остановлен</b>"
        if name:
            head += f"\n{esc(_short(name, 40))}"
        lines = [head, "",
                 f"🍽 Съедено — {esc(_sz(eaten))}",
                 f"⚡ Скорость — {esc(_sz(s.counter.rate()))}/s"
                 f"  (в среднем {esc(_sz(eaten / elapsed))}/s)",
                 f"🖧 Выходов — {len(s.live_nodes) or s.node_count} из {s.node_count}",
                 f"🧵 Воркеров — {s.effective_workers or s.workers}"
                 + (f" из {s.workers}" if s.effective_workers and
                    s.effective_workers < s.workers else "")
                 + f" · качают {s.counter.active}",
                 f"⏱ Аптайм — {esc(_dur(elapsed))}"]
        if s.plan_total:
            lines.append(f"📦 План — {esc(_sz(s.plan_used + eaten))} / {esc(_sz(s.plan_total))}")
        if s.limit_bytes:
            lines.append(f"🎯 До стопа — {esc(_sz(max(s.limit_bytes - eaten, 0)))}")
        if eaten == 0 and s.counter.errors:
            lines += ["", f"⚠ Ошибок — {s.counter.errors}",
                      f"<code>{esc(s.counter.last_error[:60])}</code>"]
            # "General SOCKS server failure" — обёртка SOCKS-клиента: она лишь
            # говорит, что sing-box не смог выйти через ноду. Настоящая причина
            # (REALITY handshake, DNS, unreachable) — в логе самого sing-box.
            box_log = s.box.tail_log(6) if s.box else ""
            if box_log:
                lines += ["", "🔍 <b>sing-box пишет:</b>",
                          f"<code>{esc(box_log[-600:])}</code>"]
        return "\n".join(lines)

    # ---------- reports ----------
    def _save_report(self, key: dict, status: str, info: Dict[str, int], eaten: int, note: str) -> None:
        total, used, remaining = plan_summary(info)
        key["report"] = {"status": status, "total": total, "used": used, "remaining": remaining,
                         "eaten": eaten, "note": note, "ts": int(time.time())}
        self.store.save()

    def _deadreason(self, info: Dict[str, int], raw: List[dict]) -> str:
        total, used, _ = plan_summary(info)
        exp = info.get("expire")
        if exp and exp < time.time():
            return f"подписка истекла ({time.strftime('%Y-%m-%d', time.gmtime(exp))})"
        if total and used >= total:
            return "квота плана исчерпана"
        if raw:
            tag = str(raw[0].get("tag") or "").strip()
            if tag:
                return f"панель пишет: «{tag[:80]}»"
        return "нет реальных нод"

    # ---------- burn until cutoff ----------
    async def _burn(self, chat_id: int, key: dict, limit_override: int, mid: Optional[int]) -> None:
        title = key.get("name", "key")

        async def put(text: str, kb: Optional[dict]) -> None:
            nonlocal mid
            if mid:
                await self.tg.edit(chat_id, mid, text, kb)
            else:
                r = await self.tg.send(chat_id, text, kb)
                if isinstance(r, dict):
                    mid = (r.get("result") or {}).get("message_id")

        await put(f"⏳ <b>{esc(_short(title, 40))}</b>\n\nКачаю подписку…", None)

        async def on_try(n: int, tot: int) -> None:
            await put(f"⏳ <b>{esc(_short(title, 40))}</b>\n\n"
                      f"Жду реальные ноды от панели… {n + 1} из {tot}", None)

        try:
            sub_ob, raw, info, ua_used = await self._fetch_nodes(key, tries=3, delay=3.0, on_try=on_try)
        except Exception as e:
            self._save_report(key, "ошибка", {}, 0, str(e))
            await put(f"⛔ <b>{esc(_short(title, 40))}</b>\n\nОшибка загрузки\n"
                      f"<code>{esc(str(e)[:150])}</code>", self._menu_kb())
            return
        total, used, remaining = plan_summary(info)
        if not sub_ob and _all_placeholders(raw):
            self._save_report(key, "заглушки/HWID", info, 0, "панель скрыла ноды")
            i = self.store.index_of(key["url"])
            await put(f"🔒 <b>{esc(_short(title, 40))}</b>\n\n"
                      "Только заглушки, 3 попытки подряд. Подписка отдаёт ноды "
                      "лишь настоящему Happ, лимит устройств забит.\n\n"
                      "<b>Обход:</b> пришли ссылки <code>vless://</code> из Happ.",
                      _kb([[_btn("📥 Вставить ссылки vless://", "addkey")],
                           [_btn("🧹 Стереть HWID", f"clrhw:{i}")],
                           [_btn("⬅️ Меню", "menu")]]))
            return
        if not sub_ob:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, 0, reason)
            await put(f"⛔ <b>{esc(_short(title, 40))}</b>\n\n{esc(reason)}", self._menu_kb())
            return
        if _all_placeholders(sub_ob):
            self._save_report(key, "заглушки/HWID", info, 0, "панель скрыла ноды")
            i = self.store.index_of(key["url"])
            await put(f"🔒 <b>{esc(_short(title, 40))}</b>\n\n"
                      "Панель отдала только заглушки — ноды скрыты для всех "
                      "клиентов, кроме настоящего Happ.\n\n"
                      "<b>Обход:</b> пришли ссылки <code>vless://</code> из Happ — "
                      "заведётся ручной ключ, без панели и HWID.\n\n"
                      "🎲 не поможет: каждый новый HWID занимает слот устройства.",
                      _kb([[_btn("📥 Вставить ссылки vless://", "addkey")],
                           [_btn("🆔 Задать HWID вручную", f"hwid:{i}")],
                           [_btn("⬅️ Меню", "menu")]]))
            return
        sub_ob = _filter_geo(sub_ob, key.get("country"))
        if key.get("country"):
            title = f"{title} · {key['country']}"

        limit = limit_override
        agents = list(self.store.servers)
        self.session.workers = self._workers()
        try:
            await self.session.start(sub_ob, limit, self._files(), title=title,
                                     plan_total=total, plan_used=used, auto_limit=False)
        except Exception as e:
            await self.session.stop()
            self._save_report(key, "ошибка старта", info, 0, str(e))
            box_log = self.session.box.tail_log(5) if self.session.box else ""
            msg = (f"⛔ <b>{esc(_short(title, 40))}</b>\n\n{esc(str(e)[:200])}")
            if box_log:
                msg += f"\n\n🔍 <b>sing-box пишет:</b>\n<code>{esc(box_log[-500:])}</code>"
            await put(msg, self._menu_kb())
            return

        ok_agents = []
        run_flags: Dict[str, bool] = {}
        agent_hwid = (key.get("hwid") or self.cfg["hwid"])
        for a in agents:
            res = await self._agent_call(a, "POST", "/burn", {
                "url": key["url"], "hwid": agent_hwid, "workers": self._workers(), "limit_bytes": limit})
            if res and res.get("ok"):
                ok_agents.append(a)
                run_flags[a["name"]] = True

        ab: Dict[str, int] = {}

        def render() -> str:
            t = [self._status_card(title)]
            if ok_agents:
                local = self.session.counter.bytes if self.session.counter else 0
                t += ["", f"🌐 <b>Серверы</b> · {len(ok_agents)}"]
                for a in ok_agents:
                    t.append(f"• {esc(_short(a['name'], 26))} — {esc(_sz(ab.get(a['name'], 0)))}")
                t.append(f"\nΣ <b>Всего — {esc(_sz(local + sum(ab.values())))}</b>")
            return "\n".join(t)

        async def poll() -> None:
            for a in ok_agents:
                s = await self._agent_call(a, "GET", "/stats")
                if s:
                    ab[a["name"]] = int(s.get("eaten", 0))
                    run_flags[a["name"]] = bool(s.get("running"))

        await put(render(), self._running_kb())
        the_mid = mid

        async def _live() -> None:
            try:
                while self.session.running() or any(run_flags.values()):
                    await asyncio.sleep(15)
                    await poll()
                    if the_mid:
                        await self.tg.edit(chat_id, the_mid, render(), self._running_kb())
                    if self._stop_all:
                        if self.session.running():
                            await self.session.stop()
                        for a in ok_agents:
                            await self._agent_call(a, "POST", "/stop")
                        return
            except asyncio.CancelledError:
                pass

        live = asyncio.create_task(_live())
        try:
            await live
        except BaseException:
            pass
        if self.session.running():
            await self.session.stop()
        for a in ok_agents:
            await self._agent_call(a, "POST", "/stop")
        await poll()

        local = self.session.counter.bytes if self.session.counter else 0
        total_eaten = local + sum(ab.values())
        self._save_report(key, "остановлен" if self._stop_all else "выеден/отключён", info, total_eaten, "")
        verdict = "⏹ Остановлено" if self._stop_all else "🏁 Нода отключилась или выедено"
        await put(render() + f"\n\n{verdict}\nИтог — <b>{esc(_sz(total_eaten))}</b>",
                  self._menu_kb())

    async def _burn_distributed(self, chat_id: int, mid: Optional[int]) -> None:
        keys = self.store.keys
        agents = self.store.servers

        async def put(text: str, kb: Optional[dict]) -> None:
            nonlocal mid
            if mid:
                await self.tg.edit(chat_id, mid, text, kb)
            else:
                r = await self.tg.send(chat_id, text, kb)
                if isinstance(r, dict):
                    mid = (r.get("result") or {}).get("message_id")

        if not keys:
            await put("🧩 нет ключей.", self._menu_kb())
            return
        if not agents:
            await put("🧩 нет доп. серверов.", self._menu_kb())
            return
        await put("🧩 <b>Распределённый жор</b>\n\nРаздаю ключи по серверам…", None)
        local_key = keys[0]
        self.session.workers = self._workers()
        lob, lrem, linfo = await self._key_load(local_key)
        started_local = False
        if lob:
            lt, lu, _ = plan_summary(linfo)
            try:
                await self.session.start(lob, 0, self._files(), title=local_key["name"],
                                         plan_total=lt, plan_used=lu, auto_limit=False)
                started_local = True
            except Exception:
                started_local = False

        active = []
        skipped = []
        for a in agents:
            ku = a.get("key_url")
            k = (self.store.key_by_url(ku) if ku else None) or local_key
            ob, rem, info = await self._key_load(k)
            if not ob:
                skipped.append(f"{a['name']}({k['name']})")
                continue
            res = await self._agent_call(a, "POST", "/burn", {
                "url": k["url"], "hwid": self.cfg["hwid"], "workers": self._workers(), "limit_bytes": 0})
            if res and res.get("ok"):
                active.append((a, k["name"]))
            else:
                skipped.append(a["name"])

        ab: Dict[str, int] = {}
        run_flags: Dict[str, bool] = {a["name"]: True for a, _ in active}

        def render() -> str:
            local = self.session.counter.bytes if self.session.counter else 0
            this = local_key["name"] if started_local else "—"
            t = ["🧩 <b>Распределённый жор</b>", "",
                 f"🖥 Эта машина · {esc(_short(this, 22))} — {esc(_sz(local))}"]
            for a, kn in active:
                t.append(f"🌐 {esc(_short(a['name'], 20))} · {esc(_short(kn, 20))} — "
                         f"{esc(_sz(ab.get(a['name'], 0)))}")
            t.append(f"\nΣ <b>Всего — {esc(_sz(local + sum(ab.values())))}</b>")
            if skipped:
                t.append(f"Пропущено — {len(skipped)}")
            return "\n".join(t)

        async def poll() -> None:
            for a, _ in active:
                s = await self._agent_call(a, "GET", "/stats")
                if s:
                    ab[a["name"]] = int(s.get("eaten", 0))
                    run_flags[a["name"]] = bool(s.get("running"))

        await put(render(), self._running_kb())
        the_mid = mid

        async def _live() -> None:
            try:
                while self.session.running() or any(run_flags.values()):
                    await asyncio.sleep(15)
                    await poll()
                    if the_mid:
                        await self.tg.edit(chat_id, the_mid, render(), self._running_kb())
                    if self._stop_all:
                        if self.session.running():
                            await self.session.stop()
                        for a, _ in active:
                            await self._agent_call(a, "POST", "/stop")
                        return
            except asyncio.CancelledError:
                pass

        live = asyncio.create_task(_live())
        try:
            await live
        except BaseException:
            pass
        if self.session.running():
            await self.session.stop()
        for a, _ in active:
            await self._agent_call(a, "POST", "/stop")
        await poll()
        local = self.session.counter.bytes if self.session.counter else 0
        grand = local + sum(ab.values())
        tail = f"\n\n🏁 Готово\nИтог — <b>{esc(_sz(grand))}</b>"
        if skipped:
            tail += "\nПропущены — " + esc(", ".join(skipped))
        await put(render() + tail, self._menu_kb())

    async def _run_indices(self, chat_id: int, indices: List[int], limit_override: int, mid: Optional[int]) -> None:
        if self.busy:
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "⏳ <b>Уже работаю</b>\n\nСначала останови текущий запуск.",
                                   self._running_kb())
            return
        self.busy = True
        self._stop_all = False
        try:
            done = 0
            for i in indices:
                if self._stop_all:
                    break
                key = self.store.get(i)
                if not key:
                    continue
                await self._burn(chat_id, key, limit_override, mid)
                done += 1
            if len(indices) > 1 and mid:
                await self.tg.edit(chat_id, mid,
                                   f"🏁 <b>Готово</b> · ключей {done}\n\n" + self._menu_text(),
                                   self._menu_kb())
        finally:
            self.busy = False

    async def _run_distributed(self, chat_id: int, mid: Optional[int]) -> None:
        if self.busy:
            if mid:
                await self.tg.edit(chat_id, mid, "уже работаю. Останови сначала.", self._running_kb())
            return
        self.busy = True
        self._stop_all = False
        try:
            await self._burn_distributed(chat_id, mid)
        finally:
            self.busy = False

    async def _hoster_menu(self, chat_id: int, idx: int, mid: Optional[int]) -> None:
        """Кто хостит ноды: ASN и владелец сети по каждому адресу подписки."""
        key = self.store.get(idx)
        if not key:
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
            return
        name = key.get("name", "key")
        back = _kb([[_btn("⬅️ Назад", f"key:{idx}")]])
        if mid:
            await self.tg.edit(chat_id, mid,
                               f"🏢 <b>{esc(_short(name, 40))}</b>\n\nСмотрю, чьи это сервера…",
                               None)
        loop = asyncio.get_event_loop()
        try:
            ob, _ua, _raw, _info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"],
                                             hwid=(key.get("hwid") or self.cfg["hwid"]) or None))
        except Exception as e:
            if mid:
                await self.tg.edit(chat_id, mid,
                                   f"🏢 <b>{esc(_short(name, 40))}</b>\n\n⛔ Ошибка загрузки\n"
                                   f"<code>{esc(str(e)[:120])}</code>", back)
            return
        ob = [o for o in ob if not _is_placeholder(o.get("tag"))]
        if not ob:
            if mid:
                await self.tg.edit(chat_id, mid,
                                   f"🏢 <b>{esc(_short(name, 40))}</b>\n\n"
                                   "⛔ Подписка не отдала нод — смотреть нечего.", back)
            return
        try:
            groups, unknown, resolved = await loop.run_in_executor(
                None, lambda: hoster_summary(ob))
        except Exception as e:
            if mid:
                await self.tg.edit(chat_id, mid,
                                   f"🏢 <b>{esc(_short(name, 40))}</b>\n\n⛔ Не вышло опросить\n"
                                   f"<code>{esc(str(e)[:120])}</code>", back)
            return

        lines = [f"🏢 <b>Хостеры</b>\n{esc(_short(name, 40))}", "",
                 f"Нод — {len(ob)}, уникальных адресов — {resolved}", ""]
        if not groups:
            lines.append("Ничего определить не удалось.")
        for g in groups:
            share = round(100 * g["nodes"] / max(len(ob), 1))
            cc = ", ".join(sorted(g["countries"])[:8])
            lines.append(f"<b>{esc(g['org'])}</b>")
            tail = f" · {esc(g['asn'])}" if g["asn"] else ""
            lines.append(f"  {g['nodes']} нод ({share}%) · IP {len(g['ips'])}{tail}")
            if cc:
                lines.append(f"  {esc(cc)}")
            if not g["hosting"]:
                lines.append("  ⚠️ не похоже на датацентр")
            lines.append("")
        if unknown:
            lines.append(f"Не опознано нод — {unknown}")
        if mid:
            await self.tg.edit(chat_id, mid, "\n".join(lines).strip(), back)

    async def _geo_menu(self, chat_id: int, idx: int, mid: Optional[int]) -> None:
        key = self.store.get(idx)
        if not key:
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
            return
        name = key.get("name", "key")
        if mid:
            await self.tg.edit(chat_id, mid,
                               f"🌍 <b>{esc(_short(name, 40))}</b>\n\nСмотрю доступные страны…", None)
        loop = asyncio.get_event_loop()
        try:
            ob, _ua, _raw, _info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"],
                                             hwid=(key.get("hwid") or self.cfg["hwid"]) or None))
        except Exception as e:
            if mid:
                await self.tg.edit(chat_id, mid,
                                   f"🌍 <b>{esc(_short(name, 40))}</b>\n\n⛔ Ошибка загрузки\n"
                                   f"<code>{esc(str(e)[:120])}</code>",
                                   _kb([[_btn("⬅️ Назад", f"key:{idx}")]]))
            return
        countries = _countries(ob)
        cur = key.get("country")
        if not countries:
            if mid:
                await self.tg.edit(chat_id, mid,
                                   f"🌍 <b>{esc(_short(name, 40))}</b>\n\n⛔ Подписка не отдала нод.",
                                   _kb([[_btn("⬅️ Назад", f"key:{idx}")]]))
            return
        self._geo_cache[idx] = countries
        text = [f"🌍 <b>Страна</b>\n{esc(_short(name, 40))}", "",
                f"Нод в подписке — {len(ob)}",
                f"Сейчас — {esc(cur if cur else 'авто (все страны)')}", "",
                "Выбери, через какую страну жрать:"]
        rows = [[_btn(("• " if not cur else "") + "🎲 Авто (все)", f"sg:{idx}:-1")]]
        for j, c in enumerate(countries):
            n = sum(1 for o in ob if _country_of(o.get("tag")) == c)
            mark = "• " if c == cur else ""
            rows.append([_btn(f"{mark}{_short(c, 24)} · {n}", f"sg:{idx}:{j}")])
        rows.append([_btn("⬅️ Назад", f"key:{idx}")])
        if mid:
            await self.tg.edit(chat_id, mid, "\n".join(text), _kb(rows))

    async def _check(self, chat_id: int, idx: int, mid: Optional[int]) -> None:
        kbdone = _kb([[_btn("⬅️ К ключу", f"key:{idx}"), _btn("🔑 Ключи", "keys")]])

        async def out(text: str, kb: Optional[dict]) -> None:
            if mid:
                await self.tg.edit(chat_id, mid, text, kb)
            else:
                await self.tg.send(chat_id, text, kb)

        key = self.store.get(idx)
        if not key:
            await out("нет такого ключа.", kbdone)
            return
        name = key.get("name", "key")
        await out(f"🔍 <b>{esc(_short(name, 40))}</b>\n\nПроверяю…", None)

        async def on_try(n: int, tot: int) -> None:
            await out(f"🔍 <b>{esc(_short(name, 40))}</b>\n\n"
                      f"Панель добавила устройство, жду реальные ноды…\n"
                      f"Попытка {n + 1} из {tot}", None)

        try:
            outbounds, raw, info, ua_used = await self._fetch_nodes(key, tries=3, delay=3.0, on_try=on_try)
        except Exception as e:
            await out(f"⛔ <b>{esc(_short(name, 40))}</b>\n\n"
                      f"<code>{esc(str(e)[:150])}</code>", kbdone)
            return
        total, used, remaining = plan_summary(info)
        prev = key.get("report", {}).get("eaten", 0)
        if not outbounds and _all_placeholders(raw):
            self._save_report(key, "заглушки/HWID", info, prev, "панель скрыла ноды")
            names = "\n".join(f"• {esc(_country_of(o.get('tag')))}" for o in raw[:4])
            msg = (f"🔒 <b>{esc(_short(name, 40))}</b>\n\n"
                   f"Только заглушки, 3 попытки подряд:\n{names}\n\n"
                   "Подписка отдаёт ноды только настоящему Happ. HWID ни при "
                   "чём — заглушка приходит и на уже зарегистрированное "
                   "устройство, а каждый новый HWID лишь занимает слот. "
                   "Ботом её не взять.\n\n"
                   "<b>Обход:</b> пришли сами ссылки <code>vless://</code> из Happ — "
                   "заведётся ручной ключ, без панели и HWID.")
            await out(msg, _kb([[_btn("📥 Вставить ссылки vless://", "addkey")],
                                [_btn("🧹 Стереть HWID", f"clrhw:{idx}")],
                                [_btn("⬅️ К ключу", f"key:{idx}")]]))
            return
        if outbounds:
            self._save_report(key, "жив", info, prev, "")
            lines = [f"✅ <b>{esc(_short(name, 40))}</b>", "",
                     f"Жив · нод {len(outbounds)}", ""]
            v6 = 0
            for o in outbounds[:8]:
                srv = str(o.get("server", "?"))
                is6 = srv.count(":") >= 2
                v6 += is6
                lines.append(f"• {esc(_country_of(o.get('tag')))} — "
                             f"<code>{esc(srv)}:{esc(o.get('server_port', '?'))}</code>"
                             + (" ⚠️ IPv6" if is6 else ""))
            if len(outbounds) > 8:
                lines.append(f"…ещё {len(outbounds) - 8}")
            if v6:
                lines += ["", f"⚠️ {v6} нод только на IPv6. На IPv4-сервере это "
                              "«Network unreachable» — нужен сервер с IPv6."]
            msg = "\n".join(lines)
        else:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, prev, reason)
            msg = f"⛔ <b>{esc(_short(name, 40))}</b>\n\n{esc(reason)}"
        if total:
            msg += f"\n\nПлан — {esc(_sz(used))} / {esc(_sz(total))}"
        await out(msg, kbdone)

    async def _run_arg(self, chat_id: int, arg: str, mid: Optional[int]) -> None:
        keys = self.store.keys
        if not keys:
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
            return
        arg = arg.strip().lower()
        if arg in ("all", "все", "*", ""):
            if arg == "" and len(keys) > 1:
                if mid:
                    await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
                return
            indices = list(range(len(keys)))
        else:
            try:
                idx = int(arg) - 1
            except ValueError:
                if mid:
                    await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
                return
            if not (0 <= idx < len(keys)):
                if mid:
                    await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
                return
            indices = [idx]
        await self._run_indices(chat_id, indices, self._take_limit(), mid)

    async def _do_update_bot(self, chat_id: int) -> None:
        frm = local_head(self.cfg["install_dir"])[:7]
        r = await self.tg.send(chat_id, "🔄 <b>Обновляюсь</b>\n\n"
                                        "Перезапущусь примерно через минуту и напишу.")
        newmid = (r.get("result") or {}).get("message_id") if isinstance(r, dict) else None
        self.store.set_setting("pending_update", {"chat": chat_id, "mid": newmid, "from": frm})
        ok = run_self_update(self.cfg["install_dir"], self.cfg["branch"], self.cfg["service"])
        if not ok:
            self.store.set_setting("pending_update", None)
            await self.tg.send(chat_id, "⛔ Не смог запустить обновление — нет systemd-run. "
                                        "Обнови вручную по SSH.", self._settings_kb())

    async def _do_update_agents(self, chat_id: int) -> int:
        ok = 0
        for a in list(self.store.servers):
            r = await self._agent_call(a, "POST", "/update")
            if r and r.get("ok"):
                ok += 1
        return ok

    # ---------- callbacks ----------
    async def on_callback(self, chat_id: int, mid: Optional[int], data: str, cb_id: str) -> None:
        self._remember(chat_id)
        await self.tg.answer(cb_id)
        if data == "menu" and mid:
            await self.tg.edit(chat_id, mid, self._menu_text(), self._menu_kb())
        elif data == "keys" and mid:
            await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data == "servers" and mid:
            await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
        elif data == "targets" and mid:
            await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
        elif data == "toggle_custom":
            self.store.set_setting("custom_only", not self.store.settings.get("custom_only"))
            if mid:
                await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
        elif data == "settings" and mid:
            await self.tg.edit(chat_id, mid, self._settings_text(), self._settings_kb())
        elif data == "diag" and mid:
            await self.tg.edit(chat_id, mid, self._diag_text(), self._diag_kb())
        elif data == "thelp" and mid:
            await self.tg.edit(chat_id, mid, self._thelp_text(),
                               _kb([[_btn("⬅️ Источники", "targets")]]))
        elif data.startswith("key:") and mid:
            i = int(data[4:])
            if self.store.get(i):
                await self.tg.edit(chat_id, mid, self._key_text(i), self._key_kb(i))
            else:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data.startswith("srv:") and mid:
            i = int(data[4:])
            if 0 <= i < len(self.store.servers):
                await self.tg.edit(chat_id, mid, self._srv_text(i), self._srv_kb(i))
            else:
                await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
        elif data == "status":
            kb = self._running_kb() if self.session.running() else self._status_kb()
            if mid:
                await self.tg.edit(chat_id, mid, self._status_card(), kb)
        elif data == "routechk":
            asyncio.create_task(self._route_check(chat_id, mid))
        elif data == "server":
            asyncio.create_task(self._server_info(chat_id, mid))
        elif data == "spdtest":
            asyncio.create_task(self._speedtest(chat_id, mid))
        elif data == "run_all":
            asyncio.create_task(self._run_arg(chat_id, "all", mid))
        elif data == "dist_run":
            asyncio.create_task(self._run_distributed(chat_id, mid))
        elif data == "chkupd":
            asyncio.create_task(self._do_check_update(chat_id, mid))
        elif data == "update_bot":
            await self._do_update_bot(chat_id)
        elif data == "update_agents":
            n = await self._do_update_agents(chat_id)
            await self.tg.send(chat_id,
                               f"🔄 Команда обновления ушла на {n} из {len(self.store.servers)} серверов.")
        elif data == "update_all":
            n = await self._do_update_agents(chat_id)
            await self.tg.send(chat_id,
                               f"🔄 Серверам — {n} из {len(self.store.servers)}. Теперь бот…")
            await self._do_update_bot(chat_id)
        elif data == "stop":
            self._stop_all = True
            # мгновенный отклик: без него, пока идёт жор (self.busy), карточка
            # обновилась бы только на следующем тике _live — до 15 с, и кнопка
            # выглядела бы мёртвой ("я даже остановить не могу").
            if mid:
                ack = ("⏹ <b>Останавливаю…</b>\n\nСобираю итог." if self.busy
                       else self._menu_text())
                await self.tg.edit(chat_id, mid, ack, self._menu_kb())
            if self.session.running():
                await self.session.stop()
            for a in list(self.store.servers):
                await self._agent_call(a, "POST", "/stop")
        elif data == "addkey":
            self.awaiting[chat_id] = "key"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🔑 <b>Добавить ключ</b>\n\n"
                                   "Пришли одно из двух:\n\n"
                                   "• ссылку на подписку (через пробел можно HWID)\n"
                                   "• сами ссылки <code>vless://</code> / <code>vmess://</code> "
                                   "из Happ, построчно — тогда панель и HWID не нужны",
                                   _kb(BACK))
        elif data == "addtarget":
            self.awaiting[chat_id] = "target"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🎯 <b>Добавить источник</b>\n\n"
                                   "Пришли прямую ссылку на большой файл.",
                                   _kb(BACK))
        elif data == "srvadd":
            self.awaiting[chat_id] = "agent"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🖥 <b>Добавить сервер</b>\n\n"
                                   "Пришли SSH-доступ одной строкой:\n"
                                   "<code>IP ЛОГИН ПАРОЛЬ [ПОРТ]</code>\n\n"
                                   "Например: <code>1.2.3.4 root MyPass</code>\n"
                                   "Для root хватит: <code>IP ПАРОЛЬ</code>\n\n"
                                   "Бот сам зайдёт и поставит агента.",
                                   _kb(SRV_BACK))
        elif data.startswith("akey:") and mid:
            ai = int(data[5:])
            await self.tg.edit(chat_id, mid, self._akey_text(ai), self._akey_kb(ai))
        elif data.startswith("setk:"):
            _, ai_s, j_s = data.split(":")
            ai, j = int(ai_s), int(j_s)
            url = self.store.keys[j]["url"] if 0 <= j < len(self.store.keys) else ""
            self.store.set_server_key(ai, url)
            if mid:
                await self.tg.edit(chat_id, mid, self._srv_text(ai), self._srv_kb(ai))
        elif data.startswith("run:"):
            asyncio.create_task(self._run_indices(chat_id, [int(data[4:])], self._take_limit(), mid))
        elif data.startswith("host:"):
            asyncio.create_task(self._hoster_menu(chat_id, int(data[5:]), mid))
        elif data.startswith("geo:"):
            asyncio.create_task(self._geo_menu(chat_id, int(data[4:]), mid))
        elif data.startswith("hwid:"):
            idx = int(data[5:])
            key = self.store.get(idx)
            self.awaiting[chat_id] = f"hwid:{idx}"
            cur = (key or {}).get("hwid") or "не задан"
            if mid:
                await self.tg.edit(
                    chat_id, mid,
                    f"🆔 <b>HWID</b>\n{esc(_short((key or {}).get('name', '?'), 40))}\n\n"
                    f"Сейчас — {esc(cur)}\n\n"
                    "HWID — отпечаток устройства. Панель отдаёт ноды "
                    "только «своим».\n\n"
                    "🎲 <b>Случайный</b> — займёт свободный слот устройства, "
                    "часто этого хватает.\n"
                    "Либо пришли свой HWID — тот же, что у подписки в Happ.",
                    _kb([[_btn("🎲 Случайный", f"rnd:{idx}")],
                         [_btn("🧹 Стереть", f"clrhw:{idx}")],
                         [_btn("⬅️ Назад", f"key:{idx}")]]))
        elif data.startswith("rnd:"):
            idx = int(data[4:])
            key = self.store.get(idx)
            if key is not None:
                key["hwid"] = _rand_hwid()
                self.store.save()
                self.awaiting.pop(chat_id, None)
                asyncio.create_task(self._check(chat_id, idx, mid))
        elif data.startswith("clrhw:"):
            idx = int(data[6:])
            key = self.store.get(idx)
            if key is not None:
                key["hwid"] = ""
                self.store.save()
                self.awaiting.pop(chat_id, None)
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🧹 <b>HWID стёрт</b>\n\nБот больше не будет плодить "
                                   "устройства на этой панели.\n\n" + self._key_text(idx),
                                   self._key_kb(idx))
        elif data.startswith("sg:"):
            _, i_s, j_s = data.split(":")
            idx, j = int(i_s), int(j_s)
            key = self.store.get(idx)
            if key is not None:
                clist = self._geo_cache.get(idx, [])
                key["country"] = clist[j] if 0 <= j < len(clist) else None
                self.store.save()
            if mid:
                await self.tg.edit(chat_id, mid, self._key_text(idx), self._key_kb(idx))
        elif data.startswith("check:"):
            asyncio.create_task(self._check(chat_id, int(data[6:]), mid))
        elif data.startswith("delask:") and mid:
            i = int(data[7:])
            k = self.store.get(i)
            if not k:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
            else:
                text, kb = self._confirm("ключ", k.get("name", "key"), f"del:{i}", f"key:{i}")
                await self.tg.edit(chat_id, mid, text, kb)
        elif data.startswith("del:"):
            self.store.remove(int(data[4:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data.startswith("tdel:"):
            self.store.remove_target(int(data[5:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
        elif data.startswith("sdelask:") and mid:
            i = int(data[8:])
            s = self.store.servers[i] if 0 <= i < len(self.store.servers) else None
            if not s:
                await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
            else:
                text, kb = self._confirm("сервер", s.get("name", "?"), f"sdel:{i}", f"srv:{i}")
                await self.tg.edit(chat_id, mid, text, kb)
        elif data.startswith("sdel:"):
            self.store.remove_server(int(data[5:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
        elif data.startswith("sping:"):
            asyncio.create_task(self._ping(chat_id, int(data[6:]), mid))
        elif data.startswith("wrk:"):
            self.store.set_setting("workers", int(data[4:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._settings_text(), self._settings_kb())
        elif data.startswith("lim:"):
            token = data[4:]
            for t, v, _label in LIMIT_PRESETS:
                if t == token:
                    self.pending_limit = v
                    break
            if mid:
                await self.tg.edit(chat_id, mid, self._settings_text(), self._settings_kb())

    async def _ip_via(self, port: Optional[int]) -> str:
        urls = ["https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"]
        for url in urls:
            try:
                if port is None:
                    async with self.tg.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            return (await r.text()).strip()
                    continue
                from aiohttp_socks import ProxyConnector, ProxyType
                conn = ProxyConnector(proxy_type=ProxyType.SOCKS5, host="127.0.0.1", port=port, rdns=True)
                async with aiohttp.ClientSession(connector=conn) as s:
                    async with s.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            return (await r.text()).strip()
            except Exception:
                continue
        return ""

    async def _geoip(self) -> Dict[str, str]:
        try:
            async with self.tg.session.get(
                    "http://ip-api.com/json/?fields=status,country,city,isp,org,as,hosting,proxy,query",
                    timeout=aiohttp.ClientTimeout(total=12)) as r:
                j = await r.json(content_type=None)
            if isinstance(j, dict) and j.get("status") == "success":
                return {"ip": j.get("query", ""), "org": j.get("org", ""), "isp": j.get("isp", ""),
                        "as": j.get("as", ""), "country": j.get("country", ""), "city": j.get("city", ""),
                        "hosting": j.get("hosting")}
        except Exception:
            pass
        try:
            async with self.tg.session.get("https://ipwho.is/",
                                           timeout=aiohttp.ClientTimeout(total=12)) as r:
                j = await r.json(content_type=None)
            if isinstance(j, dict) and j.get("success"):
                conn = j.get("connection") or {}
                return {"ip": j.get("ip", ""), "org": conn.get("org", ""), "isp": conn.get("isp", ""),
                        "as": f"AS{conn.get('asn', '')} {conn.get('org', '')}".strip(),
                        "country": j.get("country", ""), "city": j.get("city", ""), "hosting": None}
        except Exception:
            pass
        return {}

    async def _has_ipv6(self) -> bool:
        loop = asyncio.get_event_loop()

        def _probe() -> bool:
            try:
                s = socket.create_connection(("2606:4700:4700::1111", 443), timeout=5)
                s.close()
                return True
            except Exception:
                return False
        try:
            return await loop.run_in_executor(None, _probe)
        except Exception:
            return False

    async def _server_info(self, chat_id: int, mid: Optional[int]) -> None:
        kb = _kb([[_btn("⚡ Тест скорости канала", "spdtest")],
                  [_btn("🔄 Обновить", "server"), _btn("⬅️ Диагностика", "diag")]])

        async def out(text: str, k: Optional[dict]) -> None:
            if mid:
                await self.tg.edit(chat_id, mid, text, k)
            else:
                await self.tg.send(chat_id, text, k)

        await out("🖧 <b>Сервер</b>\n\nСобираю инфо…", None)
        g = await self._geoip()
        v6 = await self._has_ipv6()
        if not g:
            await out("🖧 <b>Сервер</b>\n\n⛔ Не смог узнать — нет сети "
                      "или ip-api заблокирован.", kb)
            return
        blob = " ".join([g.get("org", ""), g.get("isp", ""), g.get("as", "")])
        head, note = _traffic_verdict(blob)
        host = g.get("org") or g.get("isp") or "?"
        loc = ", ".join(x for x in [g.get("country", ""), g.get("city", "")] if x) or "?"
        lines = ["🖧 <b>Сервер</b>", "",
                 f"IP — <code>{esc(g.get('ip', '?'))}</code>",
                 f"Хостер — {esc(host)}",
                 f"ASN — {esc(g.get('as', '?'))}",
                 f"Локация — {esc(loc)}",
                 f"IPv6 — {'есть ✅' if v6 else 'нет ❌'}",
                 "", "💸 <b>Трафик</b>", esc(head), esc(note)]
        await out("\n".join(lines), kb)

    async def _speedtest(self, chat_id: int, mid: Optional[int]) -> None:
        kb = _kb([[_btn("🔄 Ещё раз", "spdtest"), _btn("🖧 Сервер", "server")],
                  [_btn("⬅️ Диагностика", "diag")]])

        async def out(text: str, k: Optional[dict]) -> None:
            if mid:
                await self.tg.edit(chat_id, mid, text, k)
            else:
                await self.tg.send(chat_id, text, k)

        await out("⚡ <b>Тест скорости</b>\n\nКачаю ~100 МБ напрямую…", None)
        url = "https://speed.cloudflare.com/__down?bytes=104857600"
        got = 0
        t0 = time.monotonic()
        try:
            async with self.tg.session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as r:
                async for chunk in r.content.iter_chunked(1 << 20):
                    got += len(chunk)
                    if time.monotonic() - t0 > 15:
                        break
        except Exception as e:
            await out(f"⚡ <b>Тест скорости</b>\n\n⛔ Не смог\n"
                      f"<code>{esc(str(e)[:150])}</code>", kb)
            return
        dt = max(time.monotonic() - t0, 1e-6)
        mbps = got * 8 / dt / 1e6
        await out(f"⚡ <b>Скорость канала</b>\n\n"
                  f"≈ <b>{mbps:.0f} Мбит/с</b> ({esc(_sz(got / dt))}/s)\n"
                  f"Скачал {esc(_sz(got))} за {dt:.1f} с\n\n"
                  "Это прямой канал машины, не через ноду. "
                  "Реальный жор упрётся в скорость ноды подписки.", kb)

    async def _route_check(self, chat_id: int, mid: Optional[int]) -> None:
        kbback = _kb([[_btn("🔄 Ещё раз", "routechk"), _btn("⬅️ Диагностика", "diag")]])

        async def out(text: str, kb: Optional[dict]) -> None:
            if mid:
                await self.tg.edit(chat_id, mid, text, kb)
            else:
                await self.tg.send(chat_id, text, kb)

        direct = await self._ip_via(None)
        temp = None
        if self.session.running() and self.session.node_count:
            base, n = self.cfg["port"], self.session.node_count
        else:
            key = self.store.get(0)
            if not key:
                await out("🔎 <b>Маршрут</b>\n\nНет ключей. Добавь ключ и повтори.", kbback)
                return
            await out("🔎 <b>Маршрут</b>\n\nПоднимаю ноды…", None)
            loop = asyncio.get_event_loop()
            try:
                ob, _ua, _raw, _info = await loop.run_in_executor(
                    None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"],
                                                 hwid=(key.get("hwid") or self.cfg["hwid"]) or None))
            except Exception as e:
                await out(f"🔎 <b>Маршрут</b>\n\n⛔ Ошибка загрузки\n"
                          f"<code>{esc(str(e)[:150])}</code>", kbback)
                return
            if not ob:
                await out("🔎 <b>Маршрут</b>\n\n⛔ Подписка не отдала рабочих нод.", kbback)
                return
            from .singbox import SingBox, build_config
            box = SingBox(self.cfg["singbox_bin"])
            try:
                box.start(build_config(ob, socks_port=self.cfg["port"]), socks_port=self.cfg["port"])
            except Exception as e:
                await out(f"🔎 <b>Маршрут</b>\n\n⛔ sing-box не стартовал\n"
                          f"<code>{esc(str(e)[:150])}</code>", kbback)
                return
            base, n, temp = self.cfg["port"], len(ob), box
        try:
            lines = ["🔎 <b>Маршрут</b>", "",
                     f"IP машины напрямую — <code>{esc(direct or '—')}</code>", ""]
            seen = []
            for i in range(min(n, 8)):
                ip = await self._ip_via(base + i)
                seen.append(ip)
                mark = "✅" if (ip and ip != direct) else ("⚠️" if ip else "⛔")
                lines.append(f"{mark} Нода {i + 1} — <code>{esc(ip or 'нет ответа')}</code>")
            ok = any(ip and ip != direct for ip in seen)
            lines.append("")
            if ok:
                lines.append("✅ <b>Трафик идёт через подписку.</b>")
                lines.append("Если счётчик в приложении не растёт — у тебя "
                             "«умный» VPN: нужны заблокированные сайты "
                             "(🎯 Источники → «только свои»).")
            else:
                lines.append("⛔ <b>Трафик не выходит через ноды.</b>")
                lines.append("Проверь sing-box и живость нод — 🔍 в карточке ключа.")
            await out("\n".join(lines), kbback)
        finally:
            if temp:
                temp.stop()

    async def _ping(self, chat_id: int, idx: int, mid: Optional[int] = None) -> None:
        s = self.store.servers[idx] if 0 <= idx < len(self.store.servers) else None
        if not s:
            return

        async def out(text: str) -> None:
            if mid:
                await self.tg.edit(chat_id, mid, text, self._srv_kb(idx))
            else:
                await self.tg.send(chat_id, text, self._srv_kb(idx))

        res = await self._agent_call(s, "GET", "/ping")
        head = f"🖥 <b>{esc(_short(s.get('name', ''), 40))}</b>\n\n"
        if not (res and res.get("ok")):
            await out(head + "⛔ Не отвечает.\n\nПроверь IP, порт 8787, файрвол и токен.")
            return
        st = await self._agent_call(s, "GET", "/stats") or {}
        lines = [head + "✅ Онлайн"]
        if st.get("running"):
            lines.append(f"🔥 Жрёт — {esc(_sz(st.get('eaten', 0)))} · нод {st.get('nodes', 0)}")
        else:
            lines.append("💤 Простаивает")
        await out("\n".join(lines))

    # ---------- update watcher ----------
    async def _heads(self):
        loop = asyncio.get_event_loop()
        loc = await loop.run_in_executor(None, lambda: local_head(self.cfg["install_dir"]))
        rem = await loop.run_in_executor(None, lambda: remote_head(self.cfg["install_dir"], self.cfg["branch"]))
        return loc, rem

    async def update_watcher(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                loc, rem = await self._heads()
            except Exception:
                continue
            if rem and loc and rem != loc and rem != self._notified_head and self.chats:
                self._notified_head = rem
                kb = _kb([[_btn("⬆️ Обновить", "update_bot"), _btn("Позже", "menu")]])
                for c in list(self.chats):
                    await self.tg.send(c, f"🔔 <b>Новая версия бота</b>\n\n"
                                          f"<code>{esc(rem[:7])}</code>\n\nЖми — обновлю сам.", kb)

    async def _do_check_update(self, chat_id: int, mid: Optional[int]) -> None:
        async def out(text, kb):
            if mid:
                await self.tg.edit(chat_id, mid, text, kb)
            else:
                await self.tg.send(chat_id, text, kb)
        await out("🔄 <b>Обновления</b>\n\nСмотрю GitHub…", None)
        try:
            loc, rem = await self._heads()
        except Exception as e:
            await out(f"🔄 <b>Обновления</b>\n\n⛔ Ошибка\n"
                      f"<code>{esc(str(e)[:150])}</code>", self._settings_kb())
            return
        if not rem:
            await out("🔄 <b>Обновления</b>\n\n⛔ Не достучался до GitHub — "
                      "проверь git и сеть на сервере.", self._settings_kb())
            return
        if loc and rem == loc:
            await out(f"✅ <b>Последняя версия</b>\n\nКоммит <code>{esc(loc[:7])}</code>",
                      self._settings_kb())
        else:
            kb = _kb([[_btn("⬆️ Обновить сейчас", "update_bot")],
                      [_btn("⬅️ Настройки", "settings")]])
            await out(f"🆕 <b>Есть обновление</b>\n\n"
                      f"У тебя — <code>{esc(loc[:7] or '?')}</code>\n"
                      f"В репозитории — <code>{esc(rem[:7])}</code>\n\n"
                      "Жми — обновлю сам, SSH не нужен.", kb)

    # ---------- messages ----------
    async def dispatch(self, chat_id: int, text: str) -> None:
        self._remember(chat_id)
        aw = self.awaiting.pop(chat_id, None)
        if aw and aw.startswith("hwid:"):
            idx = int(aw.split(":")[1])
            key = self.store.get(idx)
            if not key:
                await self.tg.send(chat_id, "Этого ключа уже нет.", self._keys_kb())
                return
            val = text.strip().split()[0]
            key["hwid"] = "" if val == "-" else val
            self.store.save()
            shown = key["hwid"] or "стёрт"
            await self.tg.send(chat_id, f"✅ HWID сохранён — <code>{esc(shown)}</code>\n\nПроверяю…")
            asyncio.create_task(self._check(chat_id, idx, None))
            return
        if aw == "key":
            if _looks_like_config(text) and not _extract_url(text):
                await self._add_raw_key(chat_id, text)
                return
            url = _extract_url(text)
            if not url:
                self.awaiting[chat_id] = "key"
                await self.tg.send(chat_id, "Это не ссылка. Пришли URL подписки или сами "
                                            "ссылки <code>vless://</code> из Happ.", _kb(BACK))
                return
            hwid = ""
            for p in text.split():
                if p != url and not p.lower().startswith("http"):
                    hwid = p
                    break
            k = self.store.add(url, hwid=hwid, name=default_name(url))
            await self.tg.send(chat_id, f"✅ Ключ добавлен — {esc(_short(k['name'], 40))}",
                               self._keys_kb())
            return
        if aw == "target":
            url = _extract_url(text)
            if not url:
                self.awaiting[chat_id] = "target"
                await self.tg.send(chat_id, "Это не ссылка. Пришли URL файла.", _kb(BACK))
                return
            added = self.store.add_target(url)
            await self.tg.send(chat_id, "✅ Источник добавлен" if added else "Такой уже есть.",
                               self._targets_kb())
            return
        if aw == "agent":
            u = _extract_url(text)
            if u:
                tok = ""
                for p in text.split():
                    if p != u and not p.lower().startswith("http"):
                        tok = p
                        break
                s = self.store.add_server(u, token=tok, name=urlparse(u).hostname or u)
                res = await self._agent_call(s, "GET", "/ping")
                ok = "✅ онлайн" if (res and res.get("ok")) else "⚠️ пока не отвечает"
                await self.tg.send(chat_id, f"✅ Сервер добавлен — {esc(_short(s['name'], 32))}\n{ok}",
                                   self._servers_kb())
                return
            ssh = _parse_ssh(text)
            if not ssh:
                self.awaiting[chat_id] = "agent"
                await self.tg.send(chat_id, "Формат: <code>IP ЛОГИН ПАРОЛЬ [ПОРТ]</code> "
                                            "или <code>IP ПАРОЛЬ</code>", _kb(SRV_BACK))
                return
            host, port, user, password = ssh
            asyncio.create_task(self._provision(chat_id, host, port, user, password))
            return

        if text.startswith("/"):
            cmd = text.split()[0].split("@", 1)[0].lower()
            if cmd == "/stop":
                self._stop_all = True
                if self.session.running():
                    await self.session.stop()
                for a in list(self.store.servers):
                    await self._agent_call(a, "POST", "/stop")
                await self.tg.send(chat_id, self._menu_text(), self._menu_kb())
                return
            if cmd == "/update":
                await self._do_update_bot(chat_id)
                return
            await self.tg.send(chat_id, self._menu_text(), self._menu_kb())
            return

        if _looks_like_config(text) and not _extract_url(text):
            await self._add_raw_key(chat_id, text)
            return
        url = _extract_url(text)
        if url:
            k = self.store.add(url, hwid="", name=default_name(url))
            idx = self.store.index_of(url)
            sent = await self.tg.send(chat_id,
                                      f"✅ Ключ добавлен — {esc(_short(k['name'], 40))}\n\nЗапускаю…")
            mid = (sent.get("result") or {}).get("message_id") if isinstance(sent, dict) else None
            asyncio.create_task(self._run_indices(chat_id, [idx], self._take_limit(), mid))
            return
        await self.tg.send(chat_id, self._menu_text(), self._menu_kb())

    async def _add_raw_key(self, chat_id: int, text: str) -> None:
        self.awaiting.pop(chat_id, None)
        loop = asyncio.get_event_loop()
        try:
            ob = await loop.run_in_executor(None, lambda: outbounds_from_body(text))
        except Exception as e:
            await self.tg.send(chat_id, f"⛔ Не разобрал ссылки\n<code>{esc(str(e)[:150])}</code>",
                               _kb(BACK))
            return
        real = [o for o in ob if not _is_placeholder(o.get("tag"))]
        if not real:
            await self.tg.send(chat_id, "⛔ Рабочих нод не нашёл.\n\nПришли ссылки "
                                        "<code>vless://</code>, <code>vmess://</code> или "
                                        "<code>trojan://</code> — построчно.", _kb(BACK))
            return
        name = f"ручной · {len(real)} нод"
        self.store.add(text, hwid="", name=name)
        await self.tg.send(chat_id, f"✅ <b>Ручной ключ добавлен</b>\n\nНод — {len(real)}\n"
                                    "Панель и HWID тут ни при чём — жми ▶️.", self._keys_kb())

    # ---------- provision over SSH ----------
    async def _provision(self, chat_id: int, host: str, port: int, user: str, password: str) -> None:
        token = secrets.token_hex(16)
        aport = 8787
        url = f"http://{host}:{aport}"
        sent = await self.tg.send(chat_id, f"🚀 <b>Ставлю агента</b>\n\n"
                                           f"<code>{esc(user)}@{esc(host)}:{port}</code>")
        mid = (sent.get("result") or {}).get("message_id") if isinstance(sent, dict) else None
        buf: List[str] = []
        last = [0.0]

        async def on_log(line: str) -> None:
            buf.append(line)
            if len(buf) > 14:
                del buf[:len(buf) - 14]
            now = time.monotonic()
            if mid and now - last[0] > 1.5:
                last[0] = now
                await self.tg.edit(chat_id, mid, f"🔧 <b>Установка · {esc(host)}</b>\n\n"
                                                 "<code>" + esc("\n".join(buf)) + "</code>")

        ok = await provision_agent(host, port, user, password, self.cfg["install_url"], aport, token, on_log)
        if ok:
            s = self.store.add_server(url, token=token, name=host)
            ping = await self._agent_call(s, "GET", "/ping")
            online = ("✅ Онлайн" if (ping and ping.get("ok"))
                      else "⚠️ Агент поставлен, но порт 8787 молчит — открой 8787/tcp")
            text = (f"✅ <b>Сервер подключён</b>\n\n{esc(host)}\n"
                    f"<code>{esc(url)}</code>\n\n{online}")
            await (self.tg.edit(chat_id, mid, text, self._servers_kb()) if mid else self.tg.send(chat_id, text, self._servers_kb()))
        else:
            text = (f"⛔ <b>Не удалось поставить агента</b>\n\n{esc(host)}\n"
                    "Проверь IP, логин, пароль и порт SSH.\n\n"
                    "<code>" + esc("\n".join(buf[-8:])) + "</code>")
            await (self.tg.edit(chat_id, mid, text, self._servers_kb()) if mid else self.tg.send(chat_id, text, self._servers_kb()))


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var required")
    branch = os.environ.get("REPO_BRANCH", "claude/traffic-consuming-bot-iuxyrf")
    cfg = {
        "workers": int(os.environ.get("WORKERS", "64")),
        "port": int(os.environ.get("SOCKS_PORT", "10808")),
        "singbox_bin": os.environ.get("SINGBOX_BIN", "sing-box"),
        "ua": os.environ.get("SUB_UA") or HAPP_UA,
        "hwid": os.environ.get("SUB_HWID", ""),
        "default_limit": units_to_bytes(os.environ.get("DEFAULT_LIMIT", "0")),
        "branch": branch,
        "install_dir": os.environ.get("INSTALL_DIR", "/opt/vpn-traffic-bot"),
        "service": os.environ.get("SERVICE_NAME", "vpn-traffic-bot"),
        "install_url": os.environ.get(
            "INSTALL_URL",
            f"https://raw.githubusercontent.com/zfd430792-coder/Vpn-/{branch}/install.sh"),
    }
    data_dir = os.environ.get("DATA_DIR", "/var/lib/vpn-traffic-bot")
    store = KeyStore(os.path.join(data_dir, "keys.json"))
    allowed_env = os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
    allowed = {c.strip() for c in allowed_env.split(",") if c.strip()}

    async with aiohttp.ClientSession() as http:
        tg = TgClient(http, token)
        me = await tg.call("getMe")
        if not me.get("ok"):
            raise SystemExit(f"getMe failed: {me}")
        print(f"bot up: @{me['result'].get('username')} | keys: {len(store.keys)} servers: {len(store.servers)}", flush=True)
        bot = Bot(tg, store, cfg)
        pu = store.settings.get("pending_update")
        if pu:
            store.set_setting("pending_update", None)
            cur = local_head(cfg["install_dir"])[:7]
            if cur and pu.get("from") and cur != pu["from"]:
                txt = f"✅ Обновление установлено.\n{pu['from']} → {cur}\nбот снова на связи."
            else:
                txt = f"♻️ Бот перезапущен (версия {cur or '?'}).\nверсия не изменилась — уже последняя?"
            try:
                if pu.get("mid"):
                    await tg.edit(pu["chat"], pu["mid"], txt, bot._menu_kb())
                else:
                    await tg.send(pu["chat"], txt, bot._menu_kb())
            except Exception:
                pass
        asyncio.create_task(bot.update_watcher())
        offset = 0
        while True:
            try:
                resp = await tg.call("getUpdates", offset=offset, timeout=30,
                                     allowed_updates=["message", "callback_query"])
            except Exception:
                await asyncio.sleep(2)
                continue
            if not resp.get("ok"):
                await asyncio.sleep(2)
                continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    cb = upd["callback_query"]
                    cbmsg = cb.get("message") or {}
                    chat_id = (cbmsg.get("chat") or {}).get("id")
                    if chat_id is None:
                        continue
                    if allowed and str(chat_id) not in allowed:
                        await tg.answer(cb.get("id", ""), "нет доступа")
                        continue
                    try:
                        await bot.on_callback(chat_id, cbmsg.get("message_id"), cb.get("data", ""), cb.get("id", ""))
                    except Exception as e:  # noqa: BLE001
                        await tg.send(chat_id, f"ошибка: {e}")
                    continue
                msg = upd.get("message") or {}
                chat = msg.get("chat") or {}
                chat_id = chat.get("id")
                text = (msg.get("text") or "").strip()
                if chat_id is None or not text:
                    continue
                if allowed and str(chat_id) not in allowed:
                    await tg.send(chat_id, "отказано: chat_id не в whitelist.")
                    continue
                try:
                    await bot.dispatch(chat_id, text)
                except Exception as e:  # noqa: BLE001
                    await tg.send(chat_id, f"ошибка: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
