import asyncio
import os
import time
from typing import List, Optional

from .report import fmt_bytes
from .singbox import SingBox, build_config
from .torrent import TorrentBurner
from .traffic import WORKERS_PER_NODE, Counter, burn, probe_nodes


class BurnSession:
    def __init__(self, workers: int, singbox_bin: str, port: int,
                 data_dir: str = "/tmp/vpn-traffic-bot"):
        self.workers = workers
        self.singbox_bin = singbox_bin
        self.port = port
        self.data_dir = data_dir
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
        self.live_nodes: List[int] = []
        self.probe_blind: bool = False
        self.effective_workers: int = 0
        self.mode: str = "http"
        self.torrent: Optional[TorrentBurner] = None

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
        # Предполётная проверка: жрать через мёртвые выходы бессмысленно —
        # воркеры будут молотить отказы, а счётчик стоять на нуле.
        full, conn = await probe_nodes("127.0.0.1", self.port, self.node_count)
        # Проба помогает выбрать лучшие выходы, но не должна мешать запуску:
        # цель проверки может быть недоступна именно через эту ноду, и живой
        # выход попал бы в мёртвые. Поэтому запускаемся всегда, а вслепую —
        # лишь когда не ответил вообще никто.
        self.probe_blind = False
        if full:
            self.live_nodes = full
        elif conn:
            self.live_nodes = conn
        else:
            self.live_nodes = list(range(self.node_count))
            self.probe_blind = True
        if self.mode == "torrent":
            # Торрент-сессия работает через один SOCKS-порт, поэтому берём
            # первый живой выход. Полосу даёт не число нод, а число пиров.
            port = self.port + self.live_nodes[0]
            self.torrent = TorrentBurner("127.0.0.1", port,
                                         os.path.join(self.data_dir, "torrent"))
            self.burn_task = asyncio.create_task(
                self._run_torrent(limit_bytes, files))
            return len(self.live_nodes)

        # Воркеров ровно столько, сколько живые ноды способны переварить:
        # выше потолка они не качают, а копят отказы.
        self.effective_workers = max(
            min(self.workers, len(self.live_nodes) * WORKERS_PER_NODE),
            min(8, self.workers))
        self.burn_task = asyncio.create_task(
            burn("127.0.0.1", self.port, self.node_count, self.effective_workers,
                 limit_bytes, files, self.counter, self.stop_event,
                 live=self.live_nodes)
        )
        return len(self.live_nodes)

    async def _run_torrent(self, limit_bytes: int, magnets: List[str]) -> None:
        """Крутит торренты и переливает их счётчики в общий Counter,
        чтобы статус, лимиты и стоп работали как для обычного жора."""
        from .torrent import burn_torrents
        loop = asyncio.get_event_loop()
        sync = asyncio.create_task(self._sync_torrent())
        try:
            await burn_torrents(self.torrent, magnets, limit_bytes, self.stop_event)
        finally:
            sync.cancel()
            try:
                await sync
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _sync_torrent(self) -> None:
        while True:
            await asyncio.sleep(2)
            b = self.torrent
            if not b or not self.counter:
                continue
            st = b.stats()
            self.counter.bytes = st["bytes"]
            self.counter.active = st["active"]
            self.counter.errors = st["errors"]
            if st["last_error"]:
                self.counter.last_error = st["last_error"]
            self.counter.sample()

    def status(self) -> str:
        if not self.counter:
            return "💤 простаиваю."
        elapsed = max(time.monotonic() - self.started_at, 1e-6)
        eaten = self.counter.bytes
        rate = self.counter.rate()
        state = "🔥 жру трафик" if self.running() else "⏹ остановлен"
        head = state + (f" — {self.title}" if self.title else "")
        live = len(self.live_nodes) or self.node_count
        lines = [head, f"🖧 выходов: {live} из {self.node_count}"]
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
        if self.torrent:
            try:
                self.torrent.stop()
            except Exception:  # noqa: BLE001
                pass
            self.torrent = None
        if self.box:
            self.box.stop()
            self.box = None
