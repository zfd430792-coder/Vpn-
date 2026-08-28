import asyncio
import os
import time
from typing import Dict, List, Optional

import aiohttp

from .loader import fetch_and_load
from .report import fmt_bytes, plan_summary, units_to_bytes
from .singbox import SingBox, build_config
from .store import KeyStore, default_name
from .traffic import BIG_FILES, Counter, burn


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


class BurnSession:
    def __init__(self, workers: int, singbox_bin: str, port: int):
        self.workers = workers
        self.singbox_bin = singbox_bin
        self.port = port
        self.box: Optional[SingBox] = None
        self.counter: Optional[Counter] = None
        self.stop_event: Optional[asyncio.Event] = None
        self.burn_task: Optional[asyncio.Task] = None
        self.started_at: float = 0.0
        self.limit_bytes: int = 0
        self.node_count: int = 0
        self.plan_total: int = 0
        self.plan_used: int = 0
        self.auto_limit: bool = False
        self.title: str = ""

    def running(self) -> bool:
        return self.burn_task is not None and not self.burn_task.done()

    async def start(self, outbounds: List[dict], limit_bytes: int, files: List[str], title: str = "",
                    plan_total: int = 0, plan_used: int = 0, auto_limit: bool = False) -> int:
        if self.running():
            raise RuntimeError("уже запущена — сначала Стоп")
        if not outbounds:
            raise RuntimeError("нет реальных нод")
        config = build_config(outbounds, socks_port=self.port)
        self.box = SingBox(binary=self.singbox_bin)
        self.box.start(config, socks_port=self.port)
        self.counter = Counter()
        self.stop_event = asyncio.Event()
        self.limit_bytes = limit_bytes
        self.plan_total = plan_total
        self.plan_used = plan_used
        self.auto_limit = auto_limit
        self.title = title
        self.started_at = time.monotonic()
        self.node_count = len(outbounds)
        self.burn_task = asyncio.create_task(
            burn("127.0.0.1", self.port, self.node_count, self.workers,
                 limit_bytes, files, self.counter, self.stop_event)
        )
        return len(outbounds)

    def status(self) -> str:
        if not self.counter:
            return "💤 простаиваю. добавь ключ и жми «Запустить»."
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        eaten = self.counter.bytes
        rate = eaten / elapsed
        state = "🔥 жру трафик" if self.running() else "⏹ остановлен"
        head = state + (f" — {self.title}" if self.title else "")
        lines = [head, f"🖧 нод: {self.node_count}"]
        if self.plan_total:
            used_now = self.plan_used + eaten
            left = max(self.plan_total - used_now, 0)
            lines += [
                "——————————",
                f"📦 план: {fmt_bytes(used_now)} / {fmt_bytes(self.plan_total)}",
                f"🔋 осталось: {fmt_bytes(left)}",
            ]
        lines.append("——————————")
        lines.append(f"🍝 съел: {fmt_bytes(eaten)}")
        if self.limit_bytes and not self.auto_limit:
            lines.append(f"🎯 до стопа: {fmt_bytes(max(self.limit_bytes - eaten, 0))}")
        lines.append(f"⚡ {fmt_bytes(rate)}/s")
        lines.append(f"⏱ {int(elapsed)}с")
        if eaten == 0 and self.counter.errors:
            lines.append(f"⚠ ошибок: {self.counter.errors} ({self.counter.last_error[:70]})")
        return "\n".join(lines)

    async def stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        task = self.burn_task
        self.burn_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        if self.box:
            self.box.stop()
            self.box = None


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

    # ---------- views ----------
    def _menu_text(self) -> str:
        st = "🔥 работаю" if self.session.running() else "💤 простаиваю"
        return (
            "🚀 VPN Traffic Bot\n"
            f"состояние: {st}\n"
            f"ключей: {len(self.store.keys)} · серверов: {len(BIG_FILES) + len(self.store.targets)} · воркеров: {self._workers()}"
        )

    def _menu_kb(self) -> dict:
        return _kb([
            [_btn("▶️ Запустить всё", "run_all"), _btn("⏹ Стоп", "stop")],
            [_btn("🔑 Ключи", "keys"), _btn("🖥 Серверы", "targets")],
            [_btn("📊 Статус", "status"), _btn("⚙️ Настройки", "settings")],
        ])

    def _keys_text(self) -> str:
        if not self.store.keys:
            return "🔑 ключей нет.\nЖми «➕ добавить» или просто кинь ссылку на подписку."
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
        for i, k in enumerate(self.store.keys):
            rows.append([
                _btn(f"▶️ {i + 1}", f"run:{i}"),
                _btn(f"🔍 {i + 1}", f"check:{i}"),
                _btn(f"🗑 {i + 1}", f"del:{i}"),
            ])
        rows.append([_btn("➕ добавить ключ", "addkey")])
        rows.append([_btn("⬅️ меню", "menu")])
        return _kb(rows)

    def _targets_text(self) -> str:
        out = [f"🖥 Серверы для скачивания (чем больше — тем больше трафика)",
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
        rows.append([_btn("➕ добавить сервер", "addtarget")])
        rows.append([_btn("⬅️ меню", "menu")])
        return _kb(rows)

    def _settings_text(self) -> str:
        lim = self.pending_limit
        lim_s = fmt_bytes(lim) if lim else "Авто (остаток плана)"
        return (
            "⚙️ Настройки\n"
            f"воркеров (параллельных загрузок): {self._workers()}\n"
            "больше = больше трафика (до потолка канала VPS)\n"
            f"лимит следующего запуска: {lim_s}"
        )

    def _settings_kb(self) -> dict:
        cur = self._workers()
        wrow = [_btn(("✅ " if n == cur else "") + str(n), f"wrk:{n}") for n in WORKER_PRESETS]
        lrow = [
            _btn("Лимит: Авто", "lim:auto"),
            _btn("100GB", "lim:100g"),
            _btn("500GB", "lim:500g"),
            _btn("1TB", "lim:1t"),
        ]
        return _kb([wrow, lrow, [_btn("⬅️ меню", "menu")]])

    def _running_kb(self) -> dict:
        return _kb([[_btn("⏹ Стоп", "stop"), _btn("🔄 Обновить", "status")]])

    # ---------- reports ----------
    def _save_report(self, key: dict, status: str, info: Dict[str, int], eaten: int, note: str) -> None:
        total, used, remaining = plan_summary(info)
        key["report"] = {
            "status": status, "total": total, "used": used, "remaining": remaining,
            "eaten": eaten, "note": note, "ts": int(time.time()),
        }
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

    # ---------- burn ----------
    async def _burn_one(self, chat_id: int, key: dict, limit_override: int) -> None:
        name = key.get("name", "key")
        url = key["url"]
        hwid = key.get("hwid") or self.cfg["hwid"]
        await self.tg.send(chat_id, f"[{name}] качаю подписку…")
        loop = asyncio.get_event_loop()
        try:
            outbounds, ua_used, raw, info = await loop.run_in_executor(
                None, lambda: fetch_and_load(url, ua=self.cfg["ua"], hwid=hwid or None)
            )
        except Exception as e:
            self._save_report(key, "ошибка", {}, 0, str(e))
            await self.tg.send(chat_id, f"[{name}] ошибка загрузки: {e}", self._menu_kb())
            return
        total, used, remaining = plan_summary(info)
        if not outbounds:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, 0, reason)
            txt = f"⛔ [{name}] реальных нод нет — {reason}"
            if info:
                txt += f"\nплан: {fmt_bytes(used)} / {fmt_bytes(total)}, осталось {fmt_bytes(remaining)}"
            await self.tg.send(chat_id, txt, self._menu_kb())
            return

        limit = limit_override
        auto = False
        if limit <= 0 and remaining > 0:
            limit = remaining
            auto = True
        self.session.workers = self._workers()
        try:
            n = await self.session.start(outbounds, limit, self._files(), title=name,
                                         plan_total=total, plan_used=used, auto_limit=auto)
        except Exception as e:
            await self.session.stop()
            self._save_report(key, "ошибка старта", info, 0, str(e))
            await self.tg.send(chat_id, f"[{name}] ошибка старта: {e}", self._menu_kb())
            return

        head = f"[{name}] нод:{n} · воркеров:{self.session.workers}"
        if auto:
            head += f" · дожру {fmt_bytes(limit)}"
        elif limit:
            head += f" · лимит {fmt_bytes(limit)}"
        sent = await self.tg.send(chat_id, head + "\n\n" + self.session.status(), self._running_kb())
        mid = (sent.get("result") or {}).get("message_id") if isinstance(sent, dict) else None

        async def _live() -> None:
            try:
                while self.session.running():
                    await asyncio.sleep(20)
                    if mid:
                        await self.tg.edit(chat_id, mid, head + "\n\n" + self.session.status(), self._running_kb())
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

        eaten = self.session.counter.bytes if self.session.counter else 0
        reached = bool(self.session.limit_bytes) and eaten >= self.session.limit_bytes
        status_word = "исчерпан — съедено всё" if reached else "остановлен"
        self._save_report(key, status_word, info, eaten, "")
        final = self.session.status()
        await self.session.stop()
        if reached:
            tail = f"\n\n✅ [{name}] съел {fmt_bytes(eaten)} — лимит/остаток достигнут."
        else:
            tail = f"\n\n⏹ [{name}] остановлен, съел {fmt_bytes(eaten)}."
        if mid:
            await self.tg.edit(chat_id, mid, head + "\n\n" + final + tail, self._menu_kb())
        else:
            await self.tg.send(chat_id, final + tail, self._menu_kb())

    async def _run_indices(self, chat_id: int, indices: List[int], limit_override: int) -> None:
        if self.busy:
            await self.tg.send(chat_id, "уже работаю. Стоп сначала.", self._running_kb())
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
                await self._burn_one(chat_id, key, limit_override)
                done += 1
            if len(indices) > 1:
                await self.tg.send(chat_id, f"🏁 готово: обработано ключей {done}.", self._menu_kb())
        finally:
            self.busy = False

    async def _check(self, chat_id: int, idxs: List[int]) -> None:
        loop = asyncio.get_event_loop()
        for i in idxs:
            key = self.store.get(i)
            if not key:
                continue
            name = key.get("name", "key")
            hwid = key.get("hwid") or self.cfg["hwid"]
            try:
                outbounds, ua_used, raw, info = await loop.run_in_executor(
                    None, lambda: fetch_and_load(key["url"], ua=self.cfg["ua"], hwid=hwid or None)
                )
            except Exception as e:
                await self.tg.send(chat_id, f"[{name}] ошибка: {e}")
                continue
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
            await self.tg.send(chat_id, msg)

    async def _run_arg(self, chat_id: int, arg: str) -> None:
        keys = self.store.keys
        if not keys:
            await self.tg.send(chat_id, "ключей нет.", self._keys_kb())
            return
        arg = arg.strip().lower()
        if arg in ("all", "все", "*", ""):
            if arg == "" and len(keys) > 1:
                await self.tg.send(chat_id, "выбери ключ кнопкой:", self._keys_kb())
                return
            indices = list(range(len(keys)))
        else:
            try:
                idx = int(arg) - 1
            except ValueError:
                await self.tg.send(chat_id, "выбери ключ кнопкой:", self._keys_kb())
                return
            if not (0 <= idx < len(keys)):
                await self.tg.send(chat_id, "нет такого ключа.", self._keys_kb())
                return
            indices = [idx]
        await self._run_indices(chat_id, indices, self._take_limit())

    # ---------- callbacks ----------
    async def on_callback(self, chat_id: int, mid: Optional[int], data: str, cb_id: str) -> None:
        await self.tg.answer(cb_id)
        if data == "menu":
            if mid:
                await self.tg.edit(chat_id, mid, self._menu_text(), self._menu_kb())
        elif data == "keys":
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data == "targets":
            if mid:
                await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
        elif data == "settings":
            if mid:
                await self.tg.edit(chat_id, mid, self._settings_text(), self._settings_kb())
        elif data == "status":
            await self.tg.send(chat_id, self.session.status(),
                               self._running_kb() if self.session.running() else self._menu_kb())
        elif data == "run_all":
            asyncio.create_task(self._run_arg(chat_id, "all"))
        elif data == "stop":
            self._stop_all = True
            if self.session.running():
                await self.session.stop()
                await self.tg.send(chat_id, "⏹ остановил.", self._menu_kb())
            else:
                await self.tg.send(chat_id, "💤 и так простаиваю.", self._menu_kb())
        elif data == "addkey":
            self.awaiting[chat_id] = "key"
            await self.tg.send(chat_id, "🔑 пришли ссылку на подписку одним сообщением (можно через пробел hwid).", _kb(BACK))
        elif data == "addtarget":
            self.awaiting[chat_id] = "target"
            await self.tg.send(chat_id, "🖥 пришли URL большого файла (чем жирнее и ближе — тем лучше).", _kb(BACK))
        elif data.startswith("run:"):
            asyncio.create_task(self._run_indices(chat_id, [int(data[4:])], self._take_limit()))
        elif data.startswith("check:"):
            asyncio.create_task(self._check(chat_id, [int(data[6:])]))
        elif data.startswith("del:"):
            self.store.remove(int(data[4:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._keys_text(), self._keys_kb())
        elif data.startswith("tdel:"):
            self.store.remove_target(int(data[5:]))
            if mid:
                await self.tg.edit(chat_id, mid, self._targets_text(), self._targets_kb())
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
            await self.tg.send(chat_id, "✅ сервер добавлен" if added else "уже есть такой.", self._targets_kb())
            return

        if text.startswith("/"):
            cmd = text.split()[0].split("@", 1)[0].lower()
            if cmd == "/stop":
                self._stop_all = True
                if self.session.running():
                    await self.session.stop()
                    await self.tg.send(chat_id, "⏹ остановил.", self._menu_kb())
                else:
                    await self.tg.send(chat_id, self._menu_text(), self._menu_kb())
                return
            if cmd == "/status":
                await self.tg.send(chat_id, self.session.status(),
                                   self._running_kb() if self.session.running() else self._menu_kb())
                return
            await self.tg.send(chat_id, self._menu_text(), self._menu_kb())
            return

        url = _extract_url(text)
        if url:
            k = self.store.add(url, hwid="", name=default_name(url))
            idx = self.store.index_of(url)
            await self.tg.send(chat_id, f"✅ ключ {k['name']} добавлен. запускаю…")
            asyncio.create_task(self._run_indices(chat_id, [idx], self._take_limit()))
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
        print(f"bot up: @{me['result'].get('username')} | keys: {len(store.keys)}", flush=True)
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
