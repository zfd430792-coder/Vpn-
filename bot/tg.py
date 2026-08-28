import asyncio
import os
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp

from .engine import BurnSession
from .loader import fetch_and_load
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

    # ---------- views ----------
    def _menu_text(self) -> str:
        st = "🔥 работаю" if self.session.running() else "💤 простаиваю"
        return (
            "🚀 VPN Traffic Bot\n"
            f"состояние: {st}\n"
            f"ключей: {len(self.store.keys)} · серверов: {len(self.store.servers)} · воркеров: {self._workers()}"
        )

    def _menu_kb(self) -> dict:
        return _kb([
            [_btn("▶️ Запустить всё", "run_all"), _btn("⏹ Стоп", "stop")],
            [_btn("🔑 Ключи", "keys"), _btn("🖥 Серверы", "servers")],
            [_btn("🎯 Источники", "targets"), _btn("📊 Статус", "status")],
            [_btn("⚙️ Настройки", "settings")],
        ])

    def _keys_text(self) -> str:
        if not self.store.keys:
            return "🔑 ключей нет.\nЖми «➕ добавить» или кинь ссылку на подписку."
        out = ["🔑 Ключи:"]
        for i, k in enumerate(self.store.keys, 1):
            line = f"{i}. {k.get('name', 'key')}"
            r = k.get("report") or {}
            if r:
                if r.get("total"):
                    line += f"\n   план {fmt_bytes(r['used'])}/{fmt_bytes(r['total'])}, ост. {fmt_bytes(r['remaining'])}"
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
            rows.append([
                _btn(f"▶️ {i + 1}", f"run:{i}"),
                _btn(f"🔍 {i + 1}", f"check:{i}"),
                _btn(f"🗑 {i + 1}", f"del:{i}"),
            ])
        rows.append([_btn("➕ добавить ключ", "addkey")])
        rows.append([_btn("⬅️ меню", "menu")])
        return _kb(rows)

    def _servers_text(self) -> str:
        if not self.store.servers:
            return ("🖥 Доп. серверов нет.\n"
                    "Это другие VPS с агентом — они жрут тот же ключ параллельно (быстрее).\n"
                    "Установи агента на VPS и добавь его сюда (URL + токен).")
        out = ["🖥 Доп. серверы (жрут параллельно):"]
        for i, s in enumerate(self.store.servers, 1):
            out.append(f"{i}. {s.get('name')} — {s.get('url')}")
        return "\n".join(out)

    def _servers_kb(self) -> dict:
        rows = []
        for i in range(len(self.store.servers)):
            rows.append([_btn(f"🔌 пинг {i + 1}", f"sping:{i}"), _btn(f"🗑 {i + 1}", f"sdel:{i}")])
        rows.append([_btn("➕ добавить сервер", "srvadd")])
        rows.append([_btn("⬅️ меню", "menu")])
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
        lim_s = fmt_bytes(lim) if lim else "Авто (остаток плана)"
        return (
            "⚙️ Настройки\n"
            f"воркеров: {self._workers()} (больше = больше трафика)\n"
            f"лимит следующего запуска: {lim_s}"
        )

    def _settings_kb(self) -> dict:
        cur = self._workers()
        wrow = [_btn(("✅ " if n == cur else "") + str(n), f"wrk:{n}") for n in WORKER_PRESETS]
        lrow = [_btn("Лимит: Авто", "lim:auto"), _btn("100GB", "lim:100g"),
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
            return "квота плана исчерпана — всё съедено"
        if raw:
            tag = str(raw[0].get("tag") or "").strip()
            if tag:
                return f"панель пишет: «{tag[:80]}»"
        return "панель отдаёт только заглушки"

    # ---------- burn with fleet ----------
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
            txt = f"⛔ [{title}] реальных нод нет — {reason}"
            if info:
                txt += f"\nплан: {fmt_bytes(used)} / {fmt_bytes(total)}, осталось {fmt_bytes(remaining)}"
            await put(txt, self._menu_kb())
            return

        if limit_override > 0:
            target = limit_override
            auto = False
        elif remaining > 0:
            target = remaining
            auto = True
        else:
            target = 0
            auto = False

        agents = list(self.store.servers)
        local_limit = 0 if agents else target
        self.session.workers = self._workers()
        try:
            n = await self.session.start(sub_ob, local_limit, self._files(), title=title,
                                         plan_total=total, plan_used=used, auto_limit=(auto and not agents))
        except Exception as e:
            await self.session.stop()
            self._save_report(key, "ошибка старта", info, 0, str(e))
            await put(f"[{title}] ошибка старта: {e}", self._menu_kb())
            return

        ok_agents = []
        for a in agents:
            res = await self._agent_call(a, "POST", "/burn", {
                "url": key["url"], "hwid": hwid, "workers": self._workers(), "limit_bytes": 0,
            })
            if res and res.get("ok"):
                ok_agents.append(a)

        head = f"[{title}] выходов:{n} · воркеров:{self.session.workers}"
        if agents:
            head += f" · серверов:{len(ok_agents)}/{len(agents)}"
        if target:
            head += f" · цель {fmt_bytes(target)}"

        ab: Dict[str, int] = {}

        def render() -> str:
            local = self.session.counter.bytes if self.session.counter else 0
            t = [head, "", self.session.status()]
            if agents:
                t.append("🌐 сервера:")
                for a in agents:
                    t.append(f"  • {a['name']}: {fmt_bytes(ab.get(a['name'], 0))}")
                tot = local + sum(ab.values())
                g = f" / {fmt_bytes(target)}" if target else ""
                t.append(f"Σ ВСЕГО: {fmt_bytes(tot)}{g}")
            return "\n".join(t)

        async def poll() -> None:
            for a in ok_agents:
                s = await self._agent_call(a, "GET", "/stats")
                ab[a["name"]] = int((s or {}).get("eaten", 0))

        await put(render(), self._running_kb())
        the_mid = mid

        async def _live() -> None:
            try:
                while self.session.running():
                    await asyncio.sleep(15)
                    await poll()
                    if the_mid:
                        await self.tg.edit(chat_id, the_mid, render(), self._running_kb())
                    local = self.session.counter.bytes if self.session.counter else 0
                    if target and (local + sum(ab.values())) >= target:
                        await self.session.stop()
                        return
            except asyncio.CancelledError:
                pass

        live = asyncio.create_task(_live())
        task = self.session.burn_task
        if task:
            try:
                await task
            except BaseException:
                pass
        live.cancel()
        for a in ok_agents:
            await self._agent_call(a, "POST", "/stop")
        await poll()

        local = self.session.counter.bytes if self.session.counter else 0
        total_eaten = local + sum(ab.values())
        reached = bool(target) and total_eaten >= target
        self._save_report(key, "исчерпан — съедено всё" if reached else "остановлен", info, total_eaten, "")
        if reached:
            tail = f"\n\n✅ [{title}] флот съел {fmt_bytes(total_eaten)} — цель достигнута."
        else:
            tail = f"\n\n⏹ [{title}] остановлен, съедено {fmt_bytes(total_eaten)}."
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
        if info:
            msg += f"\nплан: {fmt_bytes(used)} / {fmt_bytes(total)}, осталось {fmt_bytes(remaining)}"
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
        elif data == "stop":
            self._stop_all = True
            if self.session.running():
                await self.session.stop()
            elif mid:
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
                                   "🖥 пришли агента одной строкой:\nhttp://IP:8787  ТОКЕН\n(выведутся при установке агента на VPS)",
                                   _kb([[_btn("⬅️ назад", "servers")]]))
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
            await self.tg.send(chat_id, f"⛔ {s['name']} недоступен (проверь IP/порт/firewall/токен)")

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
            url = _extract_url(text)
            if not url:
                self.awaiting[chat_id] = "agent"
                await self.tg.send(chat_id, "нужен URL вида http://IP:8787 и токен.", _kb([[_btn("⬅️ назад", "servers")]]))
                return
            tok = ""
            for p in text.split():
                if p != url and not p.lower().startswith("http"):
                    tok = p
                    break
            s = self.store.add_server(url, token=tok, name=urlparse(url).hostname or url)
            res = await self._agent_call(s, "GET", "/ping")
            ok = "✅ онлайн" if (res and res.get("ok")) else "⚠ пока не отвечает"
            await self.tg.send(chat_id, f"✅ сервер добавлен: {s['name']} ({ok})", self._servers_kb())
            return

        if text.startswith("/"):
            cmd = text.split()[0].split("@", 1)[0].lower()
            if cmd == "/stop":
                self._stop_all = True
                if self.session.running():
                    await self.session.stop()
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


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var required")
    cfg = {
        "workers": int(os.environ.get("WORKERS", "64")),
        "port": int(os.environ.get("SOCKS_PORT", "10808")),
        "singbox_bin": os.environ.get("SINGBOX_BIN", "sing-box"),
        "ua": os.environ.get("SUB_UA", "v2rayN/6.42"),
        "hwid": os.environ.get("SUB_HWID", ""),
        "default_limit": units_to_bytes(os.environ.get("DEFAULT_LIMIT", "0")),
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
