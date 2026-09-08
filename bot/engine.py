import asyncio
import os
import time
from typing import List, Optional

from .report import fmt_bytes
from .singbox import SingBox, build_config
from .torrent import TorrentBurner
from .traffic import (PROBE_DEAD, WORKERS_PER_NODE, Counter, burn,
                      probe_node, probe_nodes)


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
        self.tstats: dict = {}
        self.torrent: Optional[TorrentBurner] = None
        self.torrent_node: Optional[int] = None
        self.node_switches: int = 0
        self.probe_full: List[int] = []

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
        self.probe_full = list(full)
        if full:
            self.live_nodes = full
        elif conn:
            self.live_nodes = conn
        else:
            self.live_nodes = list(range(self.node_count))
            self.probe_blind = True
        if self.mode == "torrent":
            # Торрент-сессия работает через ОДИН SOCKS-порт, и раньше сюда
            # брался live_nodes[0] — просто первая нода списка. Если она
            # мёртвая, торрент стоял намертво, хотя HTTP-режим на этой же
            # подписке работал: там воркеры разложены по всем нодам и живые
            # тянут за мёртвых. Поэтому ищем ноду, которая реально отвечает.
            idx = await self._pick_torrent_node()
            if idx is None:
                self.box.stop()
                self.box = None
                raise RuntimeError(
                    f"ни одна из {self.node_count} нод не принимает соединения — "
                    "для торрентов нужна отвечающая нода, попробуй другую страну "
                    "или запусти в режиме «качать файлы»")
            self.torrent_node = idx
            port = self.port + idx
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

    async def _pick_torrent_node(self) -> Optional[int]:
        """Нода, которая реально отвечает: торренту нужна именно рабочая,
        запасных у одной сессии нет.

        Проба уже отработала при старте, так что берём её результат вместо
        повторного перебора: на мёртвых нодах он складывался в минуты.
        """
        if self.probe_full:
            return self.probe_full[0]
        checked = await asyncio.gather(
            *[probe_node("127.0.0.1", self.port + i, timeout=6.0) for i in self.live_nodes],
            return_exceptions=True)
        for idx, res in zip(self.live_nodes, checked):
            if res != PROBE_DEAD and not isinstance(res, BaseException):
                return idx
        # Стучаться в localhost-порт бессмысленно: sing-box слушает его всегда,
        # даже когда до ноды не достучаться. Признак живой ноды — успешный
        # SOCKS CONNECT: он проходит, только если sing-box реально открыл
        # соединение до неё.
        return None

    async def _run_torrent(self, limit_bytes: int, magnets: List[str]) -> None:
        """Крутит торренты и переливает их счётчики в общий Counter, чтобы
        статус, лимиты и стоп работали как для обычного жора.

        Нода может отвечать на SOCKS, но не пропускать пиров — тогда торрент
        висит на ней без единого байта. Поэтому если за PEER_WAIT секунд ни
        один пир не подключился, пробуем следующую ноду, и так по кругу.
        """
        from .torrent import TorrentBurner, burn_torrents
        PEER_WAIT = 75
        sync = asyncio.create_task(self._sync_torrent())
        try:
            order = self.probe_full or self.live_nodes
            for attempt, idx in enumerate(order):
                if self.stop_event.is_set():
                    return
                if attempt:  # первую ноду уже подобрали и запустили выше
                    self.torrent_node = idx
                    self.torrent = TorrentBurner(
                        "127.0.0.1", self.port + idx,
                        os.path.join(self.data_dir, "torrent"))
                task = asyncio.create_task(
                    burn_torrents(self.torrent, magnets, limit_bytes, self.stop_event))
                started = time.monotonic()
                while not task.done():
                    await asyncio.sleep(3)
                    if self.stop_event.is_set():
                        break
                    st = self.torrent.stats() if self.torrent else {}
                    if st.get("peers") or st.get("bytes"):
                        await task          # пиры пошли — работаем на этой ноде
                        return
                    if time.monotonic() - started > PEER_WAIT:
                        self.node_switches += 1
                        break               # глухо — пробуем следующую ноду
                if task.done():
                    await task
                    return
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
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
            self.tstats = st
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
