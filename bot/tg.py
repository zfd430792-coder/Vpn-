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

HELP = (
    "Я ем трафик подписок. Управление целиком отсюда.\n\n"
    "Ключи (можно много):\n"
    "/add <url> [hwid] — добавить ключ (запомню)\n"
    "/keys — список ключей и отчёты\n"
    "/del <n> — удалить ключ n\n"
    "/check <n|all> — проверить остаток по ключу\n\n"
    "Жор:\n"
    "/run <n|all> — жрать по ключу n или по всем подряд\n"
    "/stop — остановить\n"
    "/status — текущий статус\n"
    "/limit 100GB — лимит для следующего /run (0 = остаток плана)\n\n"
    "Можно просто кинуть URL — добавлю и сразу запущу."
)


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

    async def start(self, outbounds: List[dict], limit_bytes: int, title: str = "",
                    plan_total: int = 0, plan_used: int = 0, auto_limit: bool = False) -> int:
        if self.running():
            raise RuntimeError("уже запущена — сначала /stop")
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
                 limit_bytes, BIG_FILES, self.counter, self.stop_event)
        )
        return len(outbounds)

    def status(self) -> str:
        if not self.counter:
            return "простаиваю. пришли URL подписки или /run."
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        eaten = self.counter.bytes
        rate = eaten / elapsed
        state = "🔥 жру трафик" if self.running() else "⏹ остановлен"
        head = f"{state}" + (f" [{self.title}]" if self.title else "")
        lines = [head, f"нод: {self.node_count}"]
        if self.plan_total:
            used_now = self.plan_used + eaten
            left = max(self.plan_total - used_now, 0)
            lines += [
                "——————————",
                f"план: {fmt_bytes(used_now)} / {fmt_bytes(self.plan_total)}",
                f"осталось по плану: {fmt_bytes(left)}",
            ]
        lines.append("——————————")
        lines.append(f"съел за сессию: {fmt_bytes(eaten)}")
        if self.limit_bytes and not self.auto_limit:
            lines.append(f"осталось до стопа: {fmt_bytes(max(self.limit_bytes - eaten, 0))}")
        lines.append(f"скорость: {fmt_bytes(rate)}/s")
        lines.append(f"аптайм: {int(elapsed)}с")
        if eaten == 0 and self.counter.errors:
            lines.append(f"⚠ ошибок: {self.counter.errors} ({self.counter.last_error[:80]})")
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
        async with self.session.post(url, json=params) as r:
            return await r.json()

    async def send(self, chat_id: int, text: str) -> None:
        try:
            await self.call("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)
        except Exception:
            pass

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id,
                            text=text, disable_web_page_preview=True)
        except Exception:
            pass


class Bot:
    def __init__(self, tg: TgClient, store: KeyStore, cfg: dict):
        self.tg = tg
        self.store = store
        self.cfg = cfg
        self.session = BurnSession(cfg["workers"], cfg["singbox_bin"], cfg["port"])
        self.busy = False
        self._stop_all = False
        self.pending_limit = cfg["default_limit"]

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

    def _keys_text(self) -> str:
        if not self.store.keys:
            return "ключей нет. добавь: /add <url> [hwid]"
        out = ["Ключи:"]
        for i, k in enumerate(self.store.keys, 1):
            line = f"{i}. {k.get('name', 'key')}"
            r = k.get("report") or {}
            if r:
                if r.get("total"):
                    line += (f"\n   план: {fmt_bytes(r['used'])} / {fmt_bytes(r['total'])}, "
                             f"осталось {fmt_bytes(r['remaining'])}")
                line += f"\n   {r.get('status', '')} · съел {fmt_bytes(r.get('eaten', 0))} · {_ago(r.get('ts'))}"
                if r.get("note"):
                    line += f"\n   ({r['note']})"
            else:
                line += "\n   ещё не запускался"
            out.append(line)
        return "\n".join(out)

    # ---------- core burn ----------
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
            await self.tg.send(chat_id, f"[{name}] ошибка загрузки: {e}")
            return
        total, used, remaining = plan_summary(info)
        if not outbounds:
            reason = self._deadreason(info, raw)
            self._save_report(key, "мёртв/исчерпан", info, 0, reason)
            txt = f"⛔ [{name}] реальных нод нет — {reason}"
            if info:
                txt += f"\nплан: использовано {fmt_bytes(used)} / {fmt_bytes(total)}, осталось {fmt_bytes(remaining)}"
            await self.tg.send(chat_id, txt)
            return

        limit = limit_override
        auto = False
        if limit <= 0 and remaining > 0:
            limit = remaining
            auto = True
        try:
            n = await self.session.start(outbounds, limit, title=name,
                                         plan_total=total, plan_used=used, auto_limit=auto)
        except Exception as e:
            await self.session.stop()
            self._save_report(key, "ошибка старта", info, 0, str(e))
            await self.tg.send(chat_id, f"[{name}] ошибка старта: {e}")
            return

        head = f"[{name}] UA:{ua_used} · нод:{n}"
        if auto:
            head += f" · дожру остаток {fmt_bytes(limit)}"
        elif limit:
            head += f" · лимит {fmt_bytes(limit)}"
        else:
            head += " · без лимита"
        sent = await self.tg.call("sendMessage", chat_id=chat_id,
                                  text=head + "\n\n" + self.session.status(),
                                  disable_web_page_preview=True)
        mid = (sent.get("result") or {}).get("message_id") if isinstance(sent, dict) else None

        async def _live() -> None:
            try:
                while self.session.running():
                    await asyncio.sleep(20)
                    if mid:
                        await self.tg.edit(chat_id, mid, head + "\n\n" + self.session.status())
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
            tail = f"\n\n✅ [{name}] съел {fmt_bytes(eaten)} — лимит/остаток достигнут, остановился."
        else:
            tail = f"\n\n⏹ [{name}] остановлен, съел {fmt_bytes(eaten)}."
        if mid:
            await self.tg.edit(chat_id, mid, head + "\n\n" + final + tail)
        else:
            await self.tg.send(chat_id, final + tail)

    async def _run_indices(self, chat_id: int, indices: List[int], limit_override: int) -> None:
        if self.busy:
            await self.tg.send(chat_id, "уже работаю. /stop сначала.")
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
                await self.tg.send(chat_id, f"готово: обработано ключей {done}.")
        finally:
            self.busy = False

    # ---------- commands ----------
    async def _cmd_check(self, chat_id: int, arg: str) -> None:
        keys = self.store.keys
        if not keys:
            await self.tg.send(chat_id, "ключей нет. /add <url>")
            return
        arg = arg.strip().lower()
        idxs = list(range(len(keys))) if arg in ("all", "все", "*", "") else None
        if idxs is None:
            try:
                idxs = [int(arg) - 1]
            except ValueError:
                await self.tg.send(chat_id, "номер? /check 1 или /check all")
                return
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
            if outbounds:
                self._save_report(key, "жив", info, key.get("report", {}).get("eaten", 0), "")
                msg = f"✅ [{name}] жив, нод: {len(outbounds)} (UA {ua_used})"
            else:
                reason = self._deadreason(info, raw)
                self._save_report(key, "мёртв/исчерпан", info, key.get("report", {}).get("eaten", 0), reason)
                msg = f"⛔ [{name}] {reason}"
            if info:
                msg += f"\nплан: использовано {fmt_bytes(used)} / {fmt_bytes(total)}, осталось {fmt_bytes(remaining)}"
            else:
                msg += "\n(панель не отдаёт квоту)"
            await self.tg.send(chat_id, msg)

    async def _cmd_run(self, chat_id: int, arg: str) -> None:
        keys = self.store.keys
        if not keys:
            await self.tg.send(chat_id, "ключей нет. /add <url>")
            return
        arg = arg.strip().lower()
        if arg in ("all", "все", "*"):
            indices = list(range(len(keys)))
        elif arg == "":
            if len(keys) == 1:
                indices = [0]
            else:
                await self.tg.send(chat_id, "укажи номер: /run 1 или /run all")
                return
        else:
            try:
                idx = int(arg) - 1
            except ValueError:
                await self.tg.send(chat_id, "номер? /run 1 или /run all")
                return
            if not (0 <= idx < len(keys)):
                await self.tg.send(chat_id, "нет такого ключа. /keys")
                return
            indices = [idx]
        limit_override = self.pending_limit
        self.pending_limit = self.cfg["default_limit"]
        await self._run_indices(chat_id, indices, limit_override)

    def _add(self, url: str, hwid: str) -> dict:
        return self.store.add(url, hwid=hwid, name=default_name(url))

    async def dispatch(self, chat_id: int, text: str) -> None:
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@", 1)[0].lower()
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                await self.tg.send(chat_id, HELP + "\n\n" + self._keys_text())
            elif cmd == "/keys":
                await self.tg.send(chat_id, self._keys_text())
            elif cmd == "/add":
                parts = arg.split()
                url = _extract_url(arg)
                if not url:
                    await self.tg.send(chat_id, "формат: /add <url> [hwid]")
                    return
                hwid = ""
                for p in parts:
                    if p != url and not p.lower().startswith("http"):
                        hwid = p
                        break
                key = self.store.add(url, hwid=hwid, name=default_name(url))
                await self.tg.send(chat_id, f"добавил: {key['name']}" + (f" (hwid {hwid})" if hwid else "") + "\n/run чтобы запустить, /keys — список")
            elif cmd == "/del":
                try:
                    idx = int(arg) - 1
                except ValueError:
                    await self.tg.send(chat_id, "номер? /del 1")
                    return
                k = self.store.remove(idx)
                await self.tg.send(chat_id, f"удалил: {k['name']}" if k else "нет такого ключа.")
            elif cmd == "/status":
                await self.tg.send(chat_id, self.session.status())
            elif cmd == "/limit":
                try:
                    self.pending_limit = units_to_bytes(arg or "0")
                except Exception as e:
                    await self.tg.send(chat_id, f"не понял лимит: {e}")
                    return
                lim = self.pending_limit
                await self.tg.send(chat_id, f"лимит: {fmt_bytes(lim) if lim else 'остаток плана / без лимита'}. применю к следующему /run.")
            elif cmd == "/stop":
                self._stop_all = True
                if self.session.running():
                    await self.session.stop()
                    await self.tg.send(chat_id, "остановил.")
                else:
                    await self.tg.send(chat_id, "и так простаиваю.")
            elif cmd == "/check":
                asyncio.create_task(self._cmd_check(chat_id, arg))
            elif cmd == "/run":
                asyncio.create_task(self._cmd_run(chat_id, arg))
            else:
                await self.tg.send(chat_id, "неизвестная команда. /help")
            return

        url = _extract_url(text)
        if not url:
            await self.tg.send(chat_id, "пришли URL подписки или /help")
            return
        key = self.store.add(url, hwid="", name=default_name(url))
        idx = self.store.index_of(url)
        await self.tg.send(chat_id, f"добавил: {key['name']}. запускаю…")
        limit_override = self.pending_limit
        self.pending_limit = self.cfg["default_limit"]
        asyncio.create_task(self._run_indices(chat_id, [idx], limit_override))


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
                resp = await tg.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
            except Exception:
                await asyncio.sleep(2)
                continue
            if not resp.get("ok"):
                await asyncio.sleep(2)
                continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
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
