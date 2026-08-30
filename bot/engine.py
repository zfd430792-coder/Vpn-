import asyncio
import time
from typing import List, Optional

from .report import fmt_bytes
from .singbox import SingBox, build_config
from .traffic import Counter, burn


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
            raise RuntimeError("already running")
        if not outbounds:
            raise RuntimeError("no outbounds")
        config = build_config(outbounds, socks_port=self.port, binary=self.singbox_bin)
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
            return "💤 простаиваю."
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        eaten = self.counter.bytes
        rate = eaten / elapsed
        state = "🔥 жру трафик" if self.running() else "⏹ остановлен"
        head = state + (f" — {self.title}" if self.title else "")
        lines = [head, f"🖧 выходов: {self.node_count}"]
        if self.plan_total:
            used_now = self.plan_used + eaten
            left = max(self.plan_total - used_now, 0)
            lines += [
                "——————————",
                f"📦 план: {fmt_bytes(used_now)} / {fmt_bytes(self.plan_total)}",
                f"🔋 осталось: {fmt_bytes(left)}",
            ]
        lines.append("——————————")
        lines.append(f"🍝 эта машина: {fmt_bytes(eaten)}")
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
