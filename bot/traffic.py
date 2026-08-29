import asyncio
import random
import time
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType


CHUNK = 1 << 20  # 1 MiB
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

    def add(self, n: int) -> None:
        self.bytes += n

    def fail(self, msg: str) -> None:
        self.errors += 1
        self.last_error = msg


def _bust(url: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}r={random.randint(0, 1_000_000_000)}"


async def _worker(idx, counter, socks_host, socks_port, limit_bytes, files, stop):
    timeout = aiohttp.ClientTimeout(total=None, sock_read=60, sock_connect=20)
    while not stop.is_set():
        if limit_bytes and counter.bytes >= limit_bytes:
            stop.set()
            return
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
                            async for chunk in resp.content.iter_chunked(CHUNK):
                                counter.add(len(chunk))
                                if stop.is_set():
                                    return
                                if limit_bytes and counter.bytes >= limit_bytes:
                                    stop.set()
                                    return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # noqa: BLE001
                        counter.fail(f"{type(e).__name__}: {e}")
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            counter.fail(f"{type(e).__name__}: {e}")
            await asyncio.sleep(0.5)


async def _stall_monitor(counter: Counter, stop: asyncio.Event, stall_seconds: int) -> None:
    last = counter.bytes
    last_t = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        cur = counter.bytes
        if cur > last:
            last = cur
            last_t = time.monotonic()
        elif time.monotonic() - last_t >= stall_seconds:
            stop.set()
            return


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
) -> None:
    stop = stop or asyncio.Event()
    node_count = max(int(node_count), 1)
    tasks = [
        asyncio.create_task(
            _worker(i, counter, socks_host, base_port + (i % node_count), limit_bytes, files, stop)
        )
        for i in range(workers)
    ]
    mon = asyncio.create_task(_stall_monitor(counter, stop, stall_seconds))
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        mon.cancel()
        for t in tasks:
            if not t.done():
                t.cancel()
