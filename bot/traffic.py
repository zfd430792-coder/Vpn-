import asyncio
import random
import time
from collections import deque
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType


CHUNK = 1 << 20  # 1 MiB
# Ноды почти всегда лимитируют число одновременных сессий на один ключ, а вся
# подписка выдаёт по одному uuid на ноду. Воркеры сверх этого потолка не
# качают, а долбят отказами: греют CPU и злят панель. Держим осмысленный
# потолок в пересчёте на живой выход.
WORKERS_PER_NODE = 24
STALL_SECONDS = 90  # нет трафика столько — считаем ноду отрезанной/мёртвой

BIG_FILES: List[str] = [
    "https://speed.cloudflare.com/__down?bytes=1073741824",
    "https://speed.hetzner.de/1GB.bin",
    "https://speed.hetzner.de/10GB.bin",
    "http://speedtest.tele2.net/10GB.zip",
    "http://speedtest.tele2.net/1GB.zip",
    "http://ipv4.download.thinkbroadband.com/1GB.zip",
    "http://proof.ovh.net/files/1Gb.dat",
    "http://proof.ovh.net/files/10Gb.dat",
    "http://lg.fra.leaseweb.net/1000mb.bin",
    "http://lg-nyc.fdcservers.net/10GBtest.zip",
    "http://cachefly.cachefly.net/100mb.test",
]


class Counter:
    def __init__(self) -> None:
        self.bytes = 0
        self.started = time.monotonic()
        self.errors = 0
        self.last_error = ""
        self.active = 0  # сколько воркеров прямо сейчас качают, а не висят в отказе
        # снимки (время, байты) для мгновенной скорости: средняя за всю сессию
        # инерционна и маскирует то, что происходит прямо сейчас
        self._hist = deque(maxlen=64)
        self._hist.append((self.started, 0))

    def sample(self) -> None:
        self._hist.append((time.monotonic(), self.bytes))

    def rate(self, window: float = 20.0) -> float:
        """Скорость за последние window секунд (не среднюю за всю сессию)."""
        now = time.monotonic()
        for t, b in self._hist:
            if now - t <= window:
                if now - t >= 0.5:
                    return (self.bytes - b) / (now - t)
                break
        return self.bytes / max(now - self.started, 1e-6)

    def add(self, n: int) -> None:
        self.bytes += n

    def fail(self, msg: str) -> None:
        self.errors += 1
        self.last_error = msg


def _bust(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}r={random.randint(0, 1_000_000_000)}"


async def _worker(idx, counter, socks_host, pool, limit_bytes, files, stop):
    timeout = aiohttp.ClientTimeout(total=None, sock_read=60, sock_connect=20)
    while not stop.is_set():
        if limit_bytes and counter.bytes >= limit_bytes:
            stop.set()
            return
        # порт берём заново каждый раз: список живых нод обновляется на ходу
        socks_port = pool.pick(idx)
        if socks_port is None:
            await asyncio.sleep(2)
            continue
        try:
            connector = ProxyConnector(proxy_type=ProxyType.SOCKS5, host=socks_host,
                                       port=socks_port, rdns=True)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
                while not stop.is_set():
                    if limit_bytes and counter.bytes >= limit_bytes:
                        stop.set()
                        return
                    url = _bust(random.choice(files))
                    try:
                        async with sess.get(url) as resp:
                            if resp.status >= 400:
                                counter.fail(f"HTTP {resp.status}")
                                await asyncio.sleep(0.2)
                                continue
                            counter.active += 1
                            try:
                                async for chunk in resp.content.iter_chunked(CHUNK):
                                    counter.add(len(chunk))
                                    if stop.is_set():
                                        return
                                    if limit_bytes and counter.bytes >= limit_bytes:
                                        stop.set()
                                        return
                            finally:
                                counter.active -= 1
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        counter.fail(f"{type(e).__name__}: {e}")
                        # Когда прокси отказывает мгновенно (нода не поднялась),
                        # пересоздание сессии без паузы превращается в busy-loop:
                        # тысячи ошибок в секунду и сожжённый CPU без единого байта.
                        await asyncio.sleep(0.5)
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            counter.fail(f"{type(e).__name__}: {e}")
            await asyncio.sleep(0.5)


async def _stall_monitor(counter: Counter, stop: asyncio.Event, stall_seconds: int,
                         host: str = "127.0.0.1", base_port: int = 0,
                         node_count: int = 0, pool: Optional["NodePool"] = None,
                         recheck_every: float = 45.0) -> None:
    last = counter.bytes
    last_t = time.monotonic()
    next_recheck = time.monotonic() + recheck_every
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        counter.sample()
        cur = counter.bytes
        if cur > last:
            last = cur
            last_t = time.monotonic()
        elif time.monotonic() - last_t >= stall_seconds:
            stop.set()
            return
        # Ноды отваливаются по ходу: без пересмотра воркеры продолжают долбить
        # мёртвые выходы, и скорость проседает без видимой причины.
        if pool is not None and node_count and time.monotonic() >= next_recheck:
            next_recheck = time.monotonic() + recheck_every
            try:
                live = await probe_nodes(host, base_port, node_count, timeout=6.0)
            except Exception:  # noqa: BLE001
                live = []
            if live:
                pool.ports = [base_port + i for i in live]


class NodePool:
    """Живые выходы. Воркер берёт порт заново при каждом переподключении,
    поэтому обновление списка на ходу само уводит их с умерших нод."""

    def __init__(self, ports: List[int]) -> None:
        self.ports = list(ports)

    def pick(self, idx: int) -> Optional[int]:
        ports = self.ports
        return ports[idx % len(ports)] if ports else None


async def probe_node(host: str, port: int, timeout: float = 8.0) -> bool:
    """Живой ли выход: SOCKS5 CONNECT через порт этой ноды.

    sing-box поднимает отдельный inbound на каждую ноду, поэтому успешный
    CONNECT на порт i означает, что туннель именно до ноды i собрался.
    """
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        if (await asyncio.wait_for(reader.readexactly(2), timeout))[:1] != b"\x05":
            return False
        target = b"speedtest.tele2.net"
        writer.write(b"\x05\x01\x00\x03" + bytes([len(target)]) + target + b"\x00\x50")
        await writer.drain()
        resp = await asyncio.wait_for(reader.readexactly(4), timeout)
        if resp[1] != 0:
            return False
        # Ответа прокси мало: некоторые клиенты подтверждают CONNECT сразу и
        # соединяются с целью лениво. Убеждаемся, что данные реально идут.
        atyp = resp[3]
        skip = {1: 6, 4: 18}.get(atyp)
        if skip is None:
            ln = await asyncio.wait_for(reader.readexactly(1), timeout)
            skip = ln[0] + 2
        await asyncio.wait_for(reader.readexactly(skip), timeout)
        writer.write(b"GET / HTTP/1.1\r\nHost: speedtest.tele2.net\r\n"
                     b"User-Agent: curl/8\r\nConnection: close\r\n\r\n")
        await writer.drain()
        head = await asyncio.wait_for(reader.read(16), timeout)
        return head.startswith(b"HTTP/")
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        return False
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            pass


async def probe_nodes(host: str, base_port: int, count: int,
                      timeout: float = 8.0) -> List[int]:
    """Индексы нод, через которые соединение реально устанавливается."""
    results = await asyncio.gather(
        *[probe_node(host, base_port + i, timeout) for i in range(count)],
        return_exceptions=True)
    return [i for i, ok in enumerate(results) if ok is True]


def _parse_socks(socks_url: str):
    p = urlparse(socks_url)
    return (p.hostname or "127.0.0.1", p.port or 10808)


async def burn(
    socks_host: str,
    base_port: int,
    node_count: int,
    workers: int,
    limit_bytes: int,
    files: List[str],
    counter: Counter,
    stop: Optional[asyncio.Event] = None,
    stall_seconds: int = STALL_SECONDS,
    live: Optional[List[int]] = None,
) -> None:
    stop = stop or asyncio.Event()
    node_count = max(int(node_count), 1)
    # live — индексы нод, прошедших предполётную проверку. Раскидываем воркеров
    # только по ним, чтобы мёртвые выходы не съедали долю параллелизма.
    pool = NodePool([base_port + i for i in (live if live else range(node_count))])
    tasks = [
        asyncio.create_task(
            _worker(i, counter, socks_host, pool, limit_bytes, files, stop)
        )
        for i in range(workers)
    ]
    mon = asyncio.create_task(
        _stall_monitor(counter, stop, stall_seconds, socks_host, base_port,
                       node_count, pool))
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        mon.cancel()
        for t in tasks:
            if not t.done():
                t.cancel()
