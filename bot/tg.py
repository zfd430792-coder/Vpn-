import asyncio
import os
import time
from typing import List, Optional

import aiohttp

from .loader import fetch_and_load
from .report import fmt_bytes, units_to_bytes
from .singbox import SingBox, build_config
from .traffic import BIG_FILES, Counter, burn


API = "https://api.telegram.org"

HELP = (
    "Пришли ссылку на подписку — начну есть трафик.\n\n"
    "Команды:\n"
    "/status — сколько уже съел\n"
    "/stop — остановить\n"
    "/limit 100GB — задать лимит для следующего запуска (0 = без лимита)\n"
    "/help — эта справка"
)


def _extract_url(text: str) -> Optional[str]:
    for token in text.split():
        low = token.lower()
        if low.startswith("http://") or low.startswith("https://"):
            return token
    return None


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

    def running(self) -> bool:
        return self.burn_task is not None and not self.burn_task.done()

    async def start(self, outbounds: List[dict], limit_bytes: int) -> int:
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
        self.started_at = time.monotonic()
        self.node_count = len(outbounds)
        socks_url = f"socks5h://127.0.0.1:{self.port}"
        self.burn_task = asyncio.create_task(
            burn(socks_url, self.workers, limit_bytes, BIG_FILES, self.counter, self.stop_event)
        )
        return len(outbounds)

    def status(self) -> str:
        if not self.counter:
            return "простаиваю. пришли URL подписки."
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        rate = self.counter.bytes / elapsed
        goal = f" / {fmt_bytes(self.limit_bytes)}" if self.limit_bytes else ""
        state = "работаю" if self.running() else "остановлен"
        lines = [
            state,
            f"нод: {self.node_count}",
            f"съедено: {fmt_bytes(self.counter.bytes)}{goal}",
            f"средняя: {fmt_bytes(rate)}/s",
            f"аптайм: {int(elapsed)}с",
        ]
        if self.counter.errors:
            lines.append(f"ошибок: {self.counter.errors} (посл.: {self.counter.last_error[:120]})")
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


async def _run_burn(tg: TgClient, chat_id: int, session: BurnSession, sub_url: str,
                    limit_bytes: int, ua: str, hwid: str) -> None:
    try:
        await tg.send(chat_id, "качаю подписку…")
        loop = asyncio.get_event_loop()
        outbounds, ua_used, raw = await loop.run_in_executor(
            None, lambda: fetch_and_load(sub_url, ua=ua, hwid=hwid or None)
        )
    except Exception as e:
        await tg.send(chat_id, f"ошибка загрузки подписки: {e}")
        return
    if not outbounds:
        hint = "" if hwid else " Задай HWID (SUB_HWID) — панель требует его."
        await tg.send(
            chat_id,
            f"подписка отдала только заглушки (пустышек: {raw}).{hint}",
        )
        return
    try:
        n = await session.start(outbounds, limit_bytes)
    except Exception as e:
        await tg.send(chat_id, f"ошибка старта: {e}")
        await session.stop()
        return
    goal = f" (лимит {fmt_bytes(limit_bytes)})" if limit_bytes else ""
    await tg.send(chat_id, f"UA: {ua_used}\nподнято {n} нод, жру трафик{goal}. /status /stop")
    task = session.burn_task
    if task:
        try:
            await task
        except BaseException:
            pass
    final = session.status()
    await session.stop()
    await tg.send(chat_id, f"готово:\n{final}")


async def _handle_command(tg: TgClient, chat_id: int, cmd: str, arg: str,
                          session: BurnSession, state: dict) -> None:
    if cmd in ("/start", "/help"):
        await tg.send(chat_id, HELP)
        return
    if cmd == "/status":
        await tg.send(chat_id, session.status())
        return
    if cmd == "/stop":
        if not session.running():
            await tg.send(chat_id, "и так простаиваю.")
            return
        await session.stop()
        await tg.send(chat_id, "остановил.")
        return
    if cmd == "/limit":
        try:
            state["pending_limit"] = units_to_bytes(arg or "0")
        except Exception as e:
            await tg.send(chat_id, f"не понял лимит: {e}")
            return
        lim = state["pending_limit"]
        await tg.send(chat_id, f"лимит: {fmt_bytes(lim) if lim else 'без лимита'}. применю к следующей подписке.")
        return
    await tg.send(chat_id, "неизвестная команда. /help")


async def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN env var required")
    workers = int(os.environ.get("WORKERS", "16"))
    port = int(os.environ.get("SOCKS_PORT", "10808"))
    singbox_bin = os.environ.get("SINGBOX_BIN", "sing-box")
    ua = os.environ.get("SUB_UA", "v2rayN/6.42")
    hwid = os.environ.get("SUB_HWID", "")
    default_limit = units_to_bytes(os.environ.get("DEFAULT_LIMIT", "0"))
    allowed_env = os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
    allowed = {c.strip() for c in allowed_env.split(",") if c.strip()}

    session = BurnSession(workers=workers, singbox_bin=singbox_bin, port=port)
    state = {"pending_limit": default_limit}

    async with aiohttp.ClientSession() as http:
        tg = TgClient(http, token)
        me = await tg.call("getMe")
        if not me.get("ok"):
            raise SystemExit(f"getMe failed: {me}")
        print(f"bot up: @{me['result'].get('username')}", flush=True)
        offset = 0
        while True:
            try:
                resp = await tg.call(
                    "getUpdates",
                    offset=offset,
                    timeout=30,
                    allowed_updates=["message"],
                )
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
                if text.startswith("/"):
                    cmd, _, arg = text.partition(" ")
                    cmd = cmd.split("@", 1)[0].lower()
                    await _handle_command(tg, chat_id, cmd, arg.strip(), session, state)
                    continue
                url = _extract_url(text)
                if not url:
                    await tg.send(chat_id, "пришли URL подписки или /help")
                    continue
                if session.running():
                    await tg.send(chat_id, "уже жру. /stop сначала, потом кидай новую.")
                    continue
                limit = state["pending_limit"]
                state["pending_limit"] = default_limit
                asyncio.create_task(_run_burn(tg, chat_id, session, url, limit, ua, hwid))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
