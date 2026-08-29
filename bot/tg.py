import asyncio
import os
import secrets
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from .engine import BurnSession
from .loader import fetch_and_load
from .provision import provision_agent
from .report import fmt_bytes, plan_summary, units_to_bytes
from .store import KeyStore, default_name
from .traffic import BIG_FILES


API = "https://api.telegram.org"

WORKER_PRESETS = [32, 64, 128, 256]
LIMIT_PRESETS = [("auto", 0), ("100g", 100 * 1024**3), ("500g", 500 * 1024**3), ("1t", 1024**4)]


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


def _btn(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def _kb(rows: List[List[dict]]) -> dict:
    return {"inline_keyboard": rows}


BACK = [[_btn("⬅️ меню", "menu")]]
SRV_BACK = [[_btn("⬅️ серверы", "servers")]]


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

    def _workers(self) -> int:
        w = self.store.settings.get("workers")
        return int(w) if w else self.cfg["workers"]

    def _files(self) -> List[str]:
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
        _, _, rem = plan_summary(info)
        return ob, rem, info

    # ---------- views ----------
    def _menu_text(self) -> str:
        st = "🔥 работаю" if self.session.running() else "💤 простаиваю"
        return ("🚀 VPN Traffic Bot\n"
                f"состояние: {st}\n"
                f"ключей: {len(self.store.keys)} · серверов: {len(self.store.servers)} · воркеров: {self._workers()}")

    def _menu_kb(self) -> dict:
        return _kb([
            [_btn("▶️ Запустить всё", "run_all"), _btn("⏹ Стоп", "stop")],
            [_btn("🔑 Ключи", "keys"), _btn("🖥 Серверы", "servers")],
            [_btn("🎯 Источники", "targets"), _btn("📊 Статус", "status")],
            [_btn("⚙️ Настройки", "settings")],
        ])

    def _keys_text(self) -> str:
        if not self.store.keys:
            return "🔑 ключей нет.\nЖми «➕ добавить» или кинь ссылку."
        out = ["🔑 Ключи:"]
        for i, k in enumerate(self.store.keys, 1):
            line = f"{i}. {k.get('name', 'key')}"
            r = k.get("report") or {}
            if r:
                if r.get("total"):
                    line += f"\n   план {fmt_bytes(r['used'])}/{fmt_bytes(r['total'])}"
                line += f"\n   {r.get('status', '')} · съел {fmt_bytes(r.get('eaten', 0))} · {_ago(r.get('ts'))}"
                if r.get("note"):
                    line += f"\n   ({r['note']})"
            else:
                line += "\n   ещё не запускался"
            out.append(line)
        return "\n".join(out)

    def _keys_kb(self) -> dict:
        rows = []
        if self.store.keys:
            rows.append([_btn("▶️ Запустить всё", "run_all")])
        for i in range(len(self.store.keys)):
            rows.append([_btn(f"▶️ {i + 1}", f"run:{i}"), _btn(f"🔍 {i + 1}", f"check:{i}"), _btn(f"🗑 {i + 1}", f"del:{i}")])
        rows.append([_btn("➕ добавить ключ", "addkey")])
        rows.append([_btn("⬅️ меню", "menu")])
        return _kb(rows)

    def _servers_text(self) -> str:
        if not self.store.servers:
            return ("🖥 Доп. серверов нет.\n"
                    "Жми «➕ добавить» — пришлёшь SSH-доступ, бот сам поставит агента.")
        out = ["🖥 Доп. серверы (→ назначенный ключ):"]
        for i, s in enumerate(self.store.servers, 1):
            ku = s.get("key_url")
            if ku:
                k = self.store.key_by_url(ku)
                kn = k["name"] if k else "?"
            else:
                kn = "общий (#1)"
            out.append(f"{i}. {s.get('name')} → {kn}")
        out.append("")
        out.append("«🧩 По назначению» — каждый жрёт свой ключ.")
        return "\n".join(out)

    def _servers_kb(self) -> dict:
        rows = []
        if self.store.servers:
            rows.append([_btn("🧩 По назначению (каждый свой ключ)", "dist_run")])
        for i in range(len(self.store.servers)):
            rows.append([_btn(f"🔑 {i + 1}", f"akey:{i}"), _btn(f"🔌 {i + 1}", f"sping:{i}"), _btn(f"🗑 {i + 1}", f"sdel:{i}")])
        rows.append([_btn("➕ добавить сервер", "srvadd")])
        rows.append([_btn("⬅️ меню", "menu")])
        return _kb(rows)

    def _akey_text(self, ai: int) -> str:
        s = self.store.servers[ai] if 0 <= ai < len(self.store.servers) else None
        return f"🔑 выбери ключ для сервера: {s.get('name') if s else '?'}"

    def _akey_kb(self, ai: int) -> dict:
        rows = []
        for j, k in enumerate(self.store.keys):
            rows.append([_btn(k.get("name", f"key{j+1}"), f"setk:{ai}:{j}")])
        rows.append([_btn("✖ снять (общий #1)", f"setk:{ai}:-1")])
        rows.append([_btn("⬅️ серверы", "servers")])
        return _kb(rows)

    def _targets_text(self) -> str:
        out = ["🎯 Источники (откуда качать)",
               f"встроенных: {len(BIG_FILES)} (Cloudflare/Hetzner/OVH/Tele2…)"]
        if self.store.targets:
            out.append("твои:")
            for i, t in enumerate(self.store.targets, 1):
                out.append(f"{i}. {t}")
        else:
            out.append("своих пока нет.")
        return "\n".join(out)

    def _targets_kb(self) -> dict:
        rows = []
        for i in range(len(self.store.targets)):
            rows.append([_btn(f"🗑 удалить {i + 1}", f"tdel:{i}")])
        rows.append([_btn("➕ добавить источник", "addtarget")])
        rows.append([_btn("⬅️ меню", "menu")])
        return _kb(rows)

    def _settings_text(self) -> str:
        lim = self.pending_limit
        lim_s = fmt_bytes(lim) if lim else "без лимита (жарит до отключения)"
        return ("⚙️ Настройки\n"
                f"воркеров: {self._workers()} (больше = больше трафика)\n"
                f"лимит следующего запуска: {lim_s}")

    def _settings_kb(self) -> dict:
        cur = self._workers()
        wrow = [_btn(("✅ " if n == cur else "") + str(n), f"wrk:{n}") for n in WORKER_PRESETS]
        lrow = [_btn("Без лимита", "lim:auto"), _btn("100GB", "lim:100g"),
                _btn("500GB", "lim:500g"), _btn("1TB", "lim:1t")]
        return _kb([wrow, lrow, [_btn("⬅️ меню", "menu")]])

    def _running_kb(self) -> dict:
        return _kb([[_btn("⏹ Стоп", "stop"), _btn("🔄 Обновить", "status")]])

    def _status_kb(self) -> dict:
        return _kb([[_btn("🔄 Обновить", "status"), _btn("⬅️ меню", "menu")]])

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

    # ---------- burn one key across fleet (until cutoff) ----------
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

        await put(f"[{title}] качаю подписку…", None)
        loop = asyncio.get_event_loop()
        try:
            sub_ob, ua_used, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"], hwid=hwid or None)
            )
        except Exception as e:
            self._save_report(key, "ошибка", {}, 0, str(e))
            await put(f"[{title}] ошибка загрузки: {e}", self._menu_kb())
            return
        total, used, remaining = plan_summary(info)
        if not sub_ob:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, 0, reason)
            await put(f"⛔ [{title}] {reason}", self._menu_kb())
            return

        limit = limit_override  # 0 = без лимита, жарим до отключения
        agents = list(self.store.servers)
        self.session.workers = self._workers()
        try:
            n = await self.session.start(sub_ob, limit, self._files(), title=title,
                                         plan_total=total, plan_used=used, auto_limit=False)
        except Exception as e:
            await self.session.stop()
            self._save_report(key, "ошибка старта", info, 0, str(e))
            await put(f"[{title}] ошибка старта: {e}", self._menu_kb())
            return

        ok_agents = []
        run_flags: Dict[str, bool] = {}
        for a in agents:
            res = await self._agent_call(a, "POST", "/burn", {
                "url": key["url"], "hwid": hwid, "workers": self._workers(), "limit_bytes": limit})
            if res and res.get("ok"):
                ok_agents.append(a)
                run_flags[a["name"]] = True

        head = f"[{title}] выходов:{n} · воркеров:{self.session.workers}"
        if agents:
            head += f" · серверов:{len(ok_agents)}/{len(agents)}"
        head += f" · лимит {fmt_bytes(limit)}" if limit else " · до отключения"
        ab: Dict[str, int] = {}

        def render() -> str:
            local = self.session.counter.bytes if self.session.counter else 0
            t = [head, "", self.session.status()]
            if ok_agents:
                t.append("🌐 сервера:")
                for a in ok_agents:
                    t.append(f"  • {a['name']}: {fmt_bytes(ab.get(a['name'], 0))}")
            if ok_agents:
                t.append(f"Σ ВСЕГО: {fmt_bytes(local + sum(ab.values()))}")
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
        self._save_report(key, "выеден/отключён" if not self._stop_all else "остановлен", info, total_eaten, "")
        tail = f"\n\n🏁 [{title}] съедено {fmt_bytes(total_eaten)}" + (" (Стоп)" if self._stop_all else " — нода отключилась/выедено")
        await put(render() + tail, self._menu_kb())

    # ---------- distributed (each agent its own key, until cutoff) ----------
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
        await put("🧩 распределяю ключи по серверам…", None)
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

        head = f"🧩 распределённый жор · серверов:{len(active)} · до отключения"
        if skipped:
            head += f" · пропущено:{len(skipped)}"
        ab: Dict[str, int] = {}
        run_flags: Dict[str, bool] = {a["name"]: True for a, _ in active}

        def render() -> str:
            local = self.session.counter.bytes if self.session.counter else 0
            t = [head, "", f"🖥 этот [{local_key['name'] if started_local else '—'}]: {fmt_bytes(local)}"]
            for a, kn in active:
                t.append(f"🌐 {a['name']} [{kn}]: {fmt_bytes(ab.get(a['name'], 0))}")
            t.append(f"Σ ВСЕГО: {fmt_bytes(local + sum(ab.values()))}")
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
        tail = f"\n\n🏁 готово. Σ съедено {fmt_bytes(grand)}."
        if skipped:
            tail += "\nпропущены: " + ", ".join(skipped)
        await put(render() + tail, self._menu_kb())

    async def _run_indices(self, chat_id: int, indices: List[int], limit_override: int, mid: Optional[int]) -> None:
        if self.busy:
            if mid:
                await self.tg.edit(chat_id, mid, "уже работаю. Стоп сначала.", self._running_kb())
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
                await self.tg.edit(chat_id, mid, "уже работаю. Стоп сначала.", self._running_kb())
            return
        self.busy = True
        self._stop_all = False
        try:
            await self._burn_distributed(chat_id, mid)
        finally:
            self.busy = False

    async def _check(self, chat_id: int, idx: int, mid: Optional[int]) -> None:
        kbdone = _kb([[_btn("⬅️ ключи", "keys"), _btn("⬅️ меню", "menu")]])

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
        await out(f"🔍 [{name}] проверяю…", None)
        loop = asyncio.get_event_loop()
        try:
            outbounds, ua_used, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"], hwid=hwid or None)
            )
        except Exception as e:
            await out(f"[{name}] ошибка: {e}", kbdone)
            return
        total, used, remaining = plan_summary(info)
        prev = key.get("report", {}).get("eaten", 0)
        if outbounds:
            self._save_report(key, "жив", info, prev, "")
            msg = f"✅ [{name}] жив, нод: {len(outbounds)}"
        else:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, prev, reason)
            msg = f"⛔ [{name}] {reason}"
        if total:
            msg += f"\nплан (инфо): {fmt_bytes(used)} / {fmt_bytes(total)}"
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

    # ---------- callbacks ----------
    async def on_callback(self, chat_id: int, mid: Optional[int], data: str, cb_id: str) -> None:
        await self.tg.answer(cb_id)
        if data == "menu" and mid:
            await self.tg.edit(chat_id, mid, self._menu_text(), self._menu_kb())
        elif data == "keys" and mid:
            await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data == "servers" and mid:
            await self.tg.edit(chat_id, mid, self._servers_text(), self._servers_kb())
        elif data == "targets" and mid:
            await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
        elif data == "settings" and mid:
            await self.tg.edit(chat_id, mid, self._settings_text(), self._settings_kb())
        elif data == "status":
            kb = self._running_kb() if self.session.running() else self._status_kb()
            if mid:
                await self.tg.edit(chat_id, mid, self.session.status(), kb)
        elif data == "run_all":
            asyncio.create_task(self._run_arg(chat_id, "all", mid))
        elif data == "dist_run":
            asyncio.create_task(self._run_distributed(chat_id, mid))
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
                await self.tg.edit(chat_id, mid, "🔑 пришли ссылку на подписку (можно через пробел hwid).", _kb(BACK))
        elif data == "addtarget":
            self.awaiting[chat_id] = "target"
            if mid:
                await self.tg.edit(chat_id, mid, "🎯 пришли URL большого файла.", _kb(BACK))
        elif data == "srvadd":
            self.awaiting[chat_id] = "agent"
            if mid:
                await self.tg.edit(chat_id, mid,
                                   "🖥 пришли SSH-доступ к VPS:\nIP ЛОГИН ПАРОЛЬ [ПОРТ]\n"
                                   "пример: 1.2.3.4 root MyPass (или IP ПАРОЛЬ если root)\nбот сам поставит агента.",
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

    # ---------- messages ----------
    async def dispatch(self, chat_id: int, text: str) -> None:
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

    # ---------- provision agent over SSH ----------
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
            online = "✅ онлайн" if (ping and ping.get("ok")) else "⚠ агент поставлен, но порт 8787 пока не отвечает (открой 8787/tcp)"
            text = f"✅ ПОДКЛЮЧЁН НОВЫЙ СЕРВЕР: {host}\n{online}\n{url}"
            if mid:
                await self.tg.edit(chat_id, mid, text, self._servers_kb())
            else:
                await self.tg.send(chat_id, text, self._servers_kb())
        else:
            text = f"⛔ не удалось поставить агента на {host}. проверь IP/логин/пароль/порт SSH.\n\n" + "\n".join(buf[-8:])
            if mid:
                await self.tg.edit(chat_id, mid, text, self._servers_kb())
            else:
                await self.tg.send(chat_id, text, self._servers_kb())


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
