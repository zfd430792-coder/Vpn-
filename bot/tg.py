import asyncio
import os
import secrets
import time
from typing import Dict, List, Optional, Set
from urllib.parse import urlparse

import aiohttp

from .engine import BurnSession
from .loader import fetch_and_load
from .provision import provision_agent
from .report import fmt_bytes, plan_summary, units_to_bytes
from .selfupdate import local_head, remote_head, run_self_update
from .store import KeyStore, default_name
from .traffic import BIG_FILES


API = "https://api.telegram.org"

WORKER_PRESETS = [32, 64, 128, 256]
LIMIT_PRESETS = [("auto", 0), ("100g", 100 * 1024**3), ("500g", 500 * 1024**3), ("1t", 1024**4)]
SEP = "━━━━━━━━━━━━━━━"


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

    async def send(self, chat_id: int, text: str, kb: Optional[dict] = None) -> dict:
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if kb:
            params["reply_markup"] = kb
        return await self.call("sendMessage", **params)

    async def edit(self, chat_id: int, mid: int, text: str, kb: Optional[dict] = None) -> None:
        params = {"chat_id": chat_id, "message_id": mid, "text": text, "disable_web_page_preview": True}
        if kb:
            params["reply_markup"] = kb
        await self.call("editMessageText", **params)

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
        self.chats: Set[int] = set()
        self._notified_head = ""
        self._geo_cache: Dict[int, List[str]] = {}

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
        loop = asyncio.get_event_loop()
        try:
            ob, ua, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"],
                                             hwid=(key.get("hwid") or self.cfg["hwid"]) or None)
            )
        except Exception:
            return [], 0, {}
        ob = _filter_geo(ob, key.get("country"))
        _, _, rem = plan_summary(info)
        return ob, rem, info

    # ---------- views ----------
    def _menu_text(self) -> str:
        st = "🔥 жрёт трафик" if self.session.running() else "💤 простой"
        return (
            "🚀  VPN TRAFFIC BOT\n"
            f"{SEP}\n"
            f"● {st}\n"
            f"🔑 ключей: {len(self.store.keys)}     🖥 серверов: {len(self.store.servers)}\n"
            f"🧵 воркеров: {self._workers()}   🎯 источников: {len(self._files())}\n"
            f"{SEP}\n"
            "выбери раздел ↓"
        )

    def _menu_kb(self) -> dict:
        return _kb([
            [_btn("▶️ Запустить всё", "run_all"), _btn("⏹ Остановить", "stop")],
            [_btn("🔑 Ключи", "keys"), _btn("🖥 Серверы", "servers")],
            [_btn("🎯 Источники", "targets"), _btn("📊 Статус", "status")],
            [_btn("🔎 Проверить маршрут", "routechk")],
            [_btn("⚙️ Настройки", "settings")],
        ])

    def _keys_text(self) -> str:
        if not self.store.keys:
            return ("🔑  КЛЮЧИ\n" + SEP + "\n\n"
                    "Пусто. Нажми «➕ Добавить ключ»\nили просто кинь ссылку на подписку.")
        out = ["🔑  КЛЮЧИ ПОДПИСОК", SEP]
        for i, k in enumerate(self.store.keys, 1):
            geo = k.get("country")
            out.append(f"{i}.  {k.get('name', 'key')}   🌍 {geo if geo else 'авто'}")
            r = k.get("report") or {}
            if r:
                st = r.get("status", "")
                line = f"     {st} · съел {_sz(r.get('eaten', 0))}"
                if r.get("ts"):
                    line += f" · {_ago(r.get('ts'))}"
                out.append(line)
                if r.get("total"):
                    out.append(f"     план {_sz(r['used'])} / {_sz(r['total'])}")
                if r.get("note"):
                    out.append(f"     ⚠ {r['note']}")
            else:
                out.append("     ещё не запускался")
        out.append(SEP)
        out.append("▶️ запуск · 🌍 страна · 🔍 проверка · 🗑 удалить")
        return "\n".join(out)

    def _keys_kb(self) -> dict:
        rows = []
        if self.store.keys:
            rows.append([_btn("▶️ Запустить все ключи", "run_all")])
        for i in range(len(self.store.keys)):
            rows.append([
                _btn(f"▶️ {i + 1}", f"run:{i}"),
                _btn(f"🌍 {i + 1}", f"geo:{i}"),
                _btn(f"🔍 {i + 1}", f"check:{i}"),
                _btn(f"🗑 {i + 1}", f"del:{i}"),
            ])
        rows.append([_btn("➕ Добавить ключ", "addkey")])
        rows.append([_btn("⬅️ Меню", "menu")])
        return _kb(rows)

    def _servers_text(self) -> str:
        if not self.store.servers:
            return ("🖥  СЕРВЕРЫ\n" + SEP + "\n\n"
                    "Доп. серверов нет.\n"
                    "«➕ Добавить сервер» → пришлёшь SSH-доступ,\n"
                    "бот сам зайдёт, поставит агента и подключит.\n"
                    "Несколько машин жрут ключи параллельно = быстрее.")
        out = ["🖥  СЕРВЕРЫ (жрут параллельно)", SEP]
        for i, s in enumerate(self.store.servers, 1):
            ku = s.get("key_url")
            kn = (self.store.key_by_url(ku) or {}).get("name", "?") if ku else "общий (ключ #1)"
            out.append(f"{i}.  {s.get('name')}")
            out.append(f"     🔑 {kn}")
        out.append(SEP)
        out.append("🔑 назначить ключ · 🔌 пинг · 🗑 удалить")
        return "\n".join(out)

    def _servers_kb(self) -> dict:
        rows = []
        if self.store.servers:
            rows.append([_btn("🧩 Раздать по назначению", "dist_run")])
        for i in range(len(self.store.servers)):
            rows.append([
                _btn(f"🔑 {i + 1}", f"akey:{i}"),
                _btn(f"🔌 {i + 1}", f"sping:{i}"),
                _btn(f"🗑 {i + 1}", f"sdel:{i}"),
            ])
        rows.append([_btn("➕ Добавить сервер", "srvadd")])
        if self.store.servers:
            rows.append([_btn("🔄 Обновить серверы", "update_agents")])
        rows.append([_btn("⬅️ Меню", "menu")])
        return _kb(rows)

    def _akey_text(self, ai: int) -> str:
        s = self.store.servers[ai] if 0 <= ai < len(self.store.servers) else None
        return f"🔑  Ключ для сервера «{s.get('name') if s else '?'}»\n{SEP}\nвыбери, что он будет жрать:"

    def _akey_kb(self, ai: int) -> dict:
        rows = []
        for j, k in enumerate(self.store.keys):
            rows.append([_btn(f"🔑 {k.get('name', f'key{j+1}')}", f"setk:{ai}:{j}")])
        rows.append([_btn("✖ Сбросить (общий #1)", f"setk:{ai}:-1")])
        rows.append([_btn("⬅️ Серверы", "servers")])
        return _kb(rows)

    def _targets_text(self) -> str:
        co = bool(self.store.settings.get("custom_only"))
        mode = "только свои" if (co and self.store.targets) else "встроенные + свои"
        out = ["🎯  ИСТОЧНИКИ (откуда качать)", SEP,
               f"режим: {mode}",
               f"встроенных: {len(BIG_FILES)}  (Cloudflare, Hetzner, OVH, Tele2…)"]
        if self.store.targets:
            out.append("")
            out.append("твои:")
            for i, t in enumerate(self.store.targets, 1):
                out.append(f"{i}.  {t[:48]}")
        else:
            out.append("своих пока нет.")
        out.append(SEP)
        out.append("⚠ «Умный» VPN (обход по городам) считает трафик")
        out.append("ТОЛЬКО к ЗАБЛОКИРОВАННЫМ сайтам. Обычные CDN он")
        out.append("пропускает бесплатно → квота не тратится.")
        out.append("Чтобы жрать такую подписку: добавь сюда крупный файл")
        out.append("с ресурса, который у тебя НЕ открывается без VPN")
        out.append("(напр. archive.org), и включи «только свои».")
        return "\n".join(out)

    def _targets_kb(self) -> dict:
        rows = []
        co = bool(self.store.settings.get("custom_only"))
        rows.append([_btn(("✅" if co else "⬜️") + " только свои источники", "toggle_custom")])
        for i in range(len(self.store.targets)):
            rows.append([_btn(f"🗑 Удалить {i + 1}", f"tdel:{i}")])
        rows.append([_btn("➕ Добавить источник", "addtarget")])
        rows.append([_btn("⬅️ Меню", "menu")])
        return _kb(rows)

    def _settings_text(self) -> str:
        lim = self.pending_limit
        lim_s = _sz(lim) if lim else "без лимита (до отключения)"
        return (
            "⚙️  НАСТРОЙКИ\n"
            f"{SEP}\n"
            f"🧵 воркеров: {self._workers()}\n"
            "     больше = больше трафика (до потолка канала)\n"
            f"🎯 лимит запуска: {lim_s}\n"
            f"{SEP}\n"
            "🔄 обновление — код тянется с GitHub сам"
        )

    def _settings_kb(self) -> dict:
        cur = self._workers()
        wrow = [_btn(("• " if n == cur else "") + str(n), f"wrk:{n}") for n in WORKER_PRESETS]
        lrow = [_btn("∞ без лимита", "lim:auto"), _btn("100GB", "lim:100g"),
                _btn("500GB", "lim:500g"), _btn("1TB", "lim:1t")]
        return _kb([
            [_btn("── воркеры ──", "settings")],
            wrow,
            [_btn("── лимит ──", "settings")],
            lrow,
            [_btn("🔄 Обновить бота", "update_bot"), _btn("🔄 Обновить всё", "update_all")],
            [_btn("⬅️ Меню", "menu")],
        ])

    def _running_kb(self) -> dict:
        return _kb([[_btn("⏹ Остановить", "stop"), _btn("🔄 Обновить экран", "status")]])

    def _status_kb(self) -> dict:
        return _kb([[_btn("🔄 Обновить экран", "status"), _btn("⬅️ Меню", "menu")]])

    def _status_card(self, title: str = "") -> str:
        s = self.session
        if not s.counter:
            return "💤  ПРОСТОЙ\n" + SEP + "\nДобавь ключ и жми ▶️."
        elapsed = max(time.monotonic() - s.started_at, 1e-6)
        eaten = s.counter.bytes
        rate = eaten / elapsed
        name = title or s.title
        head = "🔥  ЖРУ ТРАФИК" if s.running() else "⏹  ОСТАНОВЛЕН"
        if name:
            head += f" · {name}"
        lines = [head, SEP,
                 f"🍽 съедено:   {_sz(eaten)}",
                 f"⚡ скорость:  {_sz(rate)}/s",
                 f"🖧 выходов:   {s.node_count} · воркеров {s.workers}",
                 f"⏱ аптайм:    {_dur(elapsed)}"]
        if s.plan_total:
            lines.append(f"📦 план:      {_sz(s.plan_used + eaten)} / {_sz(s.plan_total)}")
        if s.limit_bytes:
            lines.append(f"🎯 до стопа:  {_sz(max(s.limit_bytes - eaten, 0))}")
        if eaten == 0 and s.counter.errors:
            lines.append(f"⚠ ошибок: {s.counter.errors} ({s.counter.last_error[:60]})")
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
        hwid = key.get("hwid") or self.cfg["hwid"]

        async def put(text: str, kb: Optional[dict]) -> None:
            nonlocal mid
            if mid:
                await self.tg.edit(chat_id, mid, text, kb)
            else:
                r = await self.tg.send(chat_id, text, kb)
                if isinstance(r, dict):
                    mid = (r.get("result") or {}).get("message_id")

        await put(f"⏳  {title}\n{SEP}\nкачаю подписку…", None)
        loop = asyncio.get_event_loop()
        try:
            sub_ob, ua_used, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"], hwid=hwid or None)
            )
        except Exception as e:
            self._save_report(key, "ошибка", {}, 0, str(e))
            await put(f"⛔  {title}\n{SEP}\nошибка загрузки: {e}", self._menu_kb())
            return
        total, used, remaining = plan_summary(info)
        if not sub_ob:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, 0, reason)
            await put(f"⛔  {title}\n{SEP}\n{reason}", self._menu_kb())
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
            await put(f"⛔  {title}\n{SEP}\nошибка старта: {e}", self._menu_kb())
            return

        ok_agents = []
        run_flags: Dict[str, bool] = {}
        for a in agents:
            res = await self._agent_call(a, "POST", "/burn", {
                "url": key["url"], "hwid": hwid, "workers": self._workers(), "limit_bytes": limit})
            if res and res.get("ok"):
                ok_agents.append(a)
                run_flags[a["name"]] = True

        ab: Dict[str, int] = {}

        def render() -> str:
            t = [self._status_card(title)]
            if ok_agents:
                local = self.session.counter.bytes if self.session.counter else 0
                t.append(SEP)
                t.append(f"🌐 сервера ({len(ok_agents)}):")
                for a in ok_agents:
                    t.append(f"   • {a['name']}: {_sz(ab.get(a['name'], 0))}")
                t.append(f"Σ ВСЕГО: {_sz(local + sum(ab.values()))}")
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
        verdict = "⏹ остановлено" if self._stop_all else "🏁 нода отключилась / выедено"
        await put(render() + f"\n{SEP}\n{verdict} · итог {_sz(total_eaten)}", self._menu_kb())

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
        await put("🧩  РАСПРЕДЕЛЁННЫЙ ЖОР\n" + SEP + "\nраздаю ключи по серверам…", None)
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
            t = ["🧩  РАСПРЕДЕЛЁННЫЙ ЖОР", SEP,
                 f"🖥 этот [{local_key['name'] if started_local else '—'}]: {_sz(local)}"]
            for a, kn in active:
                t.append(f"🌐 {a['name']} [{kn}]: {_sz(ab.get(a['name'], 0))}")
            t.append(SEP)
            t.append(f"Σ ВСЕГО: {_sz(local + sum(ab.values()))}")
            if skipped:
                t.append(f"пропущено: {len(skipped)}")
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
        tail = f"\n{SEP}\n🏁 готово · итог {_sz(grand)}"
        if skipped:
            tail += "\nпропущены: " + ", ".join(skipped)
        await put(render() + tail, self._menu_kb())

    async def _run_indices(self, chat_id: int, indices: List[int], limit_override: int, mid: Optional[int]) -> None:
        if self.busy:
            if mid:
                await self.tg.edit(chat_id, mid, "уже работаю. Останови сначала.", self._running_kb())
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
                await self.tg.edit(chat_id, mid, f"🏁 готово: ключей {done}.\n\n" + self._menu_text(), self._menu_kb())
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

    async def _geo_menu(self, chat_id: int, idx: int, mid: Optional[int]) -> None:
        key = self.store.get(idx)
        if not key:
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
            return
        name = key.get("name", "key")
        if mid:
            await self.tg.edit(chat_id, mid, f"🌍  {name}\n{SEP}\nсмотрю доступные страны…", None)
        loop = asyncio.get_event_loop()
        try:
            ob, _ua, _raw, _info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"],
                                             hwid=(key.get("hwid") or self.cfg["hwid"]) or None))
        except Exception as e:
            if mid:
                await self.tg.edit(chat_id, mid, f"🌍  {name}\n{SEP}\n⛔ ошибка загрузки: {e}",
                                   _kb([[_btn("⬅️ Ключи", "keys")]]))
            return
        countries = _countries(ob)
        cur = key.get("country")
        if not countries:
            if mid:
                await self.tg.edit(chat_id, mid, f"🌍  {name}\n{SEP}\n⛔ подписка не отдала нод.",
                                   _kb([[_btn("⬅️ Ключи", "keys")]]))
            return
        self._geo_cache[idx] = countries
        text = [f"🌍  ВЫБОР СТРАНЫ · {name}", SEP,
                f"нод в подписке: {len(ob)}",
                f"сейчас: {cur if cur else 'авто (все страны)'}",
                SEP, "выбери, через какую страну жрать:"]
        rows = [[_btn(("• " if not cur else "") + "🎲 Авто (все)", f"sg:{idx}:-1")]]
        for j, c in enumerate(countries):
            n = sum(1 for o in ob if _country_of(o.get("tag")) == c)
            mark = "• " if c == cur else ""
            rows.append([_btn(f"{mark}{c}  ({n})", f"sg:{idx}:{j}")])
        rows.append([_btn("⬅️ Ключи", "keys")])
        if mid:
            await self.tg.edit(chat_id, mid, "\n".join(text), _kb(rows))

    async def _check(self, chat_id: int, idx: int, mid: Optional[int]) -> None:
        kbdone = _kb([[_btn("⬅️ Ключи", "keys"), _btn("⬅️ Меню", "menu")]])

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
        hwid = key.get("hwid") or self.cfg["hwid"]
        await out(f"🔍  {name}\n{SEP}\nпроверяю…", None)
        loop = asyncio.get_event_loop()
        try:
            outbounds, ua_used, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"], hwid=hwid or None)
            )
        except Exception as e:
            await out(f"⛔  {name}\n{SEP}\nошибка: {e}", kbdone)
            return
        total, used, remaining = plan_summary(info)
        prev = key.get("report", {}).get("eaten", 0)
        if outbounds:
            self._save_report(key, "жив", info, prev, "")
            lines = [f"✅  {name}", SEP, f"жив · нод: {len(outbounds)}", SEP]
            v6 = 0
            for o in outbounds[:8]:
                srv = str(o.get("server", "?"))
                port = o.get("server_port", "?")
                is6 = srv.count(":") >= 2
                if is6:
                    v6 += 1
                lines.append(f"• {_country_of(o.get('tag'))}: {srv}:{port}" + ("  ⚠IPv6" if is6 else ""))
            if len(outbounds) > 8:
                lines.append(f"…ещё {len(outbounds) - 8}")
            if v6:
                lines.append(SEP)
                lines.append(f"⚠ {v6} нод только на IPv6 — на IPv4-сервере это")
                lines.append("«Network unreachable». Нужен сервер с IPv6.")
            msg = "\n".join(lines)
        else:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, prev, reason)
            msg = f"⛔  {name}\n{SEP}\n{reason}"
        if total:
            msg += f"\nплан: {_sz(used)} / {_sz(total)}"
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
        await self.tg.send(chat_id, "🔄 обновляюсь до последней версии, вернусь через ~1 мин…")
        run_self_update(self.cfg["install_dir"], self.cfg["branch"], self.cfg["service"])

    async def _do_update_agents(self, chat_id: int) -> int:
        ok = 0
        for a in list(self.store.servers):
            r = await self._agent_call(a, "POST", "/update")
            if r and r.get("ok"):
                ok += 1
        return ok

    # ---------- callbacks ----------
    async def on_callback(self, chat_id: int, mid: Optional[int], data: str, cb_id: str) -> None:
        self.chats.add(chat_id)
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
        elif data == "status":
            kb = self._running_kb() if self.session.running() else self._status_kb()
            if mid:
                await self.tg.edit(chat_id, mid, self._status_card(), kb)
        elif data == "routechk":
            asyncio.create_task(self._route_check(chat_id, mid))
        elif data == "run_all":
            asyncio.create_task(self._run_arg(chat_id, "all", mid))
        elif data == "dist_run":
            asyncio.create_task(self._run_distributed(chat_id, mid))
        elif data == "update_bot":
            await self._do_update_bot(chat_id)
        elif data == "update_agents":
            n = await self._do_update_agents(chat_id)
            await self.tg.send(chat_id, f"🔄 команда обновления отправлена {n}/{len(self.store.servers)} серверам.")
        elif data == "update_all":
            n = await self._do_update_agents(chat_id)
            await self.tg.send(chat_id, f"🔄 серверам: {n}/{len(self.store.servers)}. теперь бот…")
            await self._do_update_bot(chat_id)
        elif data == "stop":
            self._stop_all = True
            if self.session.running():
                await self.session.stop()
            for a in list(self.store.servers):
                await self._agent_call(a, "POST", "/stop")
            if mid and not self.busy:
                await self.tg.edit(chat_id, mid, self._menu_text(), self._menu_kb())
        elif data == "addkey":
            self.awaiting[chat_id] = "key"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🔑  Добавить ключ\n" + SEP + "\nпришли ссылку на подписку\n(можно через пробел hwid).",
                                   _kb(BACK))
        elif data == "addtarget":
            self.awaiting[chat_id] = "target"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🎯  Добавить источник\n" + SEP + "\nпришли URL большого файла.",
                                   _kb(BACK))
        elif data == "srvadd":
            self.awaiting[chat_id] = "agent"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🖥  Добавить сервер\n" + SEP + "\n"
                                   "пришли SSH-доступ одной строкой:\n"
                                   "  IP ЛОГИН ПАРОЛЬ [ПОРТ]\n"
                                   "пример:  1.2.3.4 root MyPass\n"
                                   "(root → можно  IP ПАРОЛЬ)\n"
                                   "бот сам зайдёт и поставит агента.",
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
                await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
        elif data.startswith("run:"):
            asyncio.create_task(self._run_indices(chat_id, [int(data[4:])], self._take_limit(), mid))
        elif data.startswith("geo:"):
            asyncio.create_task(self._geo_menu(chat_id, int(data[4:]), mid))
        elif data.startswith("sg:"):
            _, i_s, j_s = data.split(":")
            idx, j = int(i_s), int(j_s)
            key = self.store.get(idx)
            if key is not None:
                clist = self._geo_cache.get(idx, [])
                key["country"] = clist[j] if 0 <= j < len(clist) else None
                self.store.save()
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data.startswith("check:"):
            asyncio.create_task(self._check(chat_id, int(data[6:]), mid))
        elif data.startswith("del:"):
            self.store.remove(int(data[4:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data.startswith("tdel:"):
            self.store.remove_target(int(data[5:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
        elif data.startswith("sdel:"):
            self.store.remove_server(int(data[5:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
        elif data.startswith("sping:"):
            asyncio.create_task(self._ping(chat_id, int(data[6:])))
        elif data.startswith("wrk:"):
            self.store.set_setting("workers", int(data[4:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._settings_text(), self._settings_kb())
        elif data.startswith("lim:"):
            token = data[4:]
            for t, v in LIMIT_PRESETS:
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

    async def _route_check(self, chat_id: int, mid: Optional[int]) -> None:
        kbback = _kb([[_btn("⬅️ Меню", "menu")]])

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
                await out("🔎  ПРОВЕРКА МАРШРУТА\n" + SEP + "\nнет ключей. Добавь ключ и повтори.", kbback)
                return
            await out("🔎  ПРОВЕРКА МАРШРУТА\n" + SEP + "\nподнимаю ноды…", None)
            loop = asyncio.get_event_loop()
            try:
                ob, _ua, _raw, _info = await loop.run_in_executor(
                    None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"],
                                                 hwid=(key.get("hwid") or self.cfg["hwid"]) or None))
            except Exception as e:
                await out(f"🔎  ПРОВЕРКА МАРШРУТА\n{SEP}\n⛔ ошибка загрузки: {e}", kbback)
                return
            if not ob:
                await out(f"🔎  ПРОВЕРКА МАРШРУТА\n{SEP}\n⛔ подписка не отдала рабочих нод.", kbback)
                return
            from .singbox import SingBox, build_config
            box = SingBox(self.cfg["singbox_bin"])
            try:
                box.start(build_config(ob, socks_port=self.cfg["port"]), socks_port=self.cfg["port"])
            except Exception as e:
                await out(f"🔎  ПРОВЕРКА МАРШРУТА\n{SEP}\n⛔ sing-box не стартовал: {e}", kbback)
                return
            base, n, temp = self.cfg["port"], len(ob), box
        try:
            lines = ["🔎  ПРОВЕРКА МАРШРУТА", SEP, f"🖥 IP сервера (напрямую): {direct or '—'}", SEP]
            seen = []
            for i in range(min(n, 8)):
                ip = await self._ip_via(base + i)
                seen.append(ip)
                mark = "✅" if (ip and ip != direct) else ("⚠" if ip else "⛔")
                lines.append(f"{mark} нода {i + 1}: {ip or 'нет ответа'}")
            ok = any(ip and ip != direct for ip in seen)
            lines.append(SEP)
            if ok:
                lines.append("✅ трафик РЕАЛЬНО идёт через подписку.")
                lines.append("Если счётчик в приложении не растёт — у тебя")
                lines.append("«умный» VPN: качай ЗАБЛОКИРОВАННЫЕ сайты")
                lines.append("(🎯 Источники → «только свои»).")
            else:
                lines.append("⛔ трафик НЕ выходит через ноды.")
                lines.append("Проверь sing-box и живость нод (🔍 в Ключах).")
            await out("\n".join(lines), kbback)
        finally:
            if temp:
                temp.stop()

    async def _ping(self, chat_id: int, idx: int) -> None:
        s = self.store.servers[idx] if 0 <= idx < len(self.store.servers) else None
        if not s:
            return
        res = await self._agent_call(s, "GET", "/ping")
        st = await self._agent_call(s, "GET", "/stats")
        if res and res.get("ok"):
            run = (st or {}).get("running")
            await self.tg.send(chat_id, f"✅ {s['name']} онлайн" + (" · сейчас жрёт" if run else ""))
        else:
            await self.tg.send(chat_id, f"⛔ {s['name']} недоступен (IP/порт/firewall/токен)")

    # ---------- update watcher ----------
    async def update_watcher(self) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(1800)
            try:
                loc = await loop.run_in_executor(None, lambda: local_head(self.cfg["install_dir"]))
                rem = await loop.run_in_executor(None, lambda: remote_head(self.cfg["install_dir"], self.cfg["branch"]))
            except Exception:
                continue
            if rem and loc and rem != loc and rem != self._notified_head and self.chats:
                self._notified_head = rem
                kb = _kb([[_btn("🔄 Обновить всё", "update_all")], [_btn("позже", "menu")]])
                for c in list(self.chats):
                    await self.tg.send(c, "🔔 Доступно обновление бота.", kb)

    # ---------- messages ----------
    async def dispatch(self, chat_id: int, text: str) -> None:
        self.chats.add(chat_id)
        aw = self.awaiting.pop(chat_id, None)
        if aw == "key":
            url = _extract_url(text)
            if not url:
                self.awaiting[chat_id] = "key"
                await self.tg.send(chat_id, "это не ссылка. пришли URL подписки.", _kb(BACK))
                return
            hwid = ""
            for p in text.split():
                if p != url and not p.lower().startswith("http"):
                    hwid = p
                    break
            k = self.store.add(url, hwid=hwid, name=default_name(url))
            await self.tg.send(chat_id, f"✅ ключ добавлен: {k['name']}", self._keys_kb())
            return
        if aw == "target":
            url = _extract_url(text)
            if not url:
                self.awaiting[chat_id] = "target"
                await self.tg.send(chat_id, "это не ссылка. пришли URL файла.", _kb(BACK))
                return
            added = self.store.add_target(url)
            await self.tg.send(chat_id, "✅ источник добавлен" if added else "уже есть такой.", self._targets_kb())
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
                ok = "✅ онлайн" if (res and res.get("ok")) else "⚠ пока не отвечает"
                await self.tg.send(chat_id, f"✅ сервер добавлен: {s['name']} ({ok})", self._servers_kb())
                return
            ssh = _parse_ssh(text)
            if not ssh:
                self.awaiting[chat_id] = "agent"
                await self.tg.send(chat_id, "формат: IP ЛОГИН ПАРОЛЬ [ПОРТ]  или  IP ПАРОЛЬ", _kb(SRV_BACK))
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

        url = _extract_url(text)
        if url:
            k = self.store.add(url, hwid="", name=default_name(url))
            idx = self.store.index_of(url)
            sent = await self.tg.send(chat_id, f"✅ ключ {k['name']} добавлен. запускаю…")
            mid = (sent.get("result") or {}).get("message_id") if isinstance(sent, dict) else None
            asyncio.create_task(self._run_indices(chat_id, [idx], self._take_limit(), mid))
            return
        await self.tg.send(chat_id, self._menu_text(), self._menu_kb())

    # ---------- provision over SSH ----------
    async def _provision(self, chat_id: int, host: str, port: int, user: str, password: str) -> None:
        token = secrets.token_hex(16)
        aport = 8787
        url = f"http://{host}:{aport}"
        sent = await self.tg.send(chat_id, f"🚀 ставлю агента на {host} (SSH {user}@{host}:{port})…")
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
                await self.tg.edit(chat_id, mid, f"🔧 установка на {host}:\n" + "\n".join(buf))

        ok = await provision_agent(host, port, user, password, self.cfg["install_url"], aport, token, on_log)
        if ok:
            s = self.store.add_server(url, token=token, name=host)
            ping = await self._agent_call(s, "GET", "/ping")
            online = "✅ онлайн" if (ping and ping.get("ok")) else "⚠ агент поставлен, но порт 8787 не отвечает (открой 8787/tcp)"
            text = f"✅ ПОДКЛЮЧЁН НОВЫЙ СЕРВЕР: {host}\n{online}\n{url}"
            await (self.tg.edit(chat_id, mid, text, self._servers_kb()) if mid else self.tg.send(chat_id, text, self._servers_kb()))
        else:
            text = f"⛔ не удалось поставить агента на {host}. проверь IP/логин/пароль/порт SSH.\n\n" + "\n".join(buf[-8:])
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
        "ua": os.environ.get("SUB_UA", "v2rayN/6.42"),
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
