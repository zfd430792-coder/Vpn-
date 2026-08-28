import asyncio
import random
import time
from typing import List, Optional
from urllib.parse import urlparse

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType


BIG_FILES: List[str] = [
    "https://speed.cloudflare.com/__down?bytes=1073741824",
    "https://speed.cloudflare.com/__down?bytes=536870912",
    "https://speed.cloudflare.com/__down?bytes=268435456",
    "http://speedtest.tele2.net/1GB.zip",
    "http://speedtest.tele2.net/10GB.zip",
    "http://ipv4.download.thinkbroadband.com/1GB.zip",
    "http://ipv4.download.thinkbroadband.com/512MB.zip",
    "http://proof.ovh.net/files/1Gb.dat",
    "http://proof.ovh.net/files/10Gb.dat",
    "http://lg.fra.leaseweb.net/1000mb.bin",
    "http://lg-nyc.fdcservers.net/1GBtest.zip",
    "http://212.183.159.230/1GB.zip",
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


async def _worker(
    idx: int,
    counter: Counter,
    socks_host: str,
    socks_port: int,
    limit_bytes: int,
    files: List[str],
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        if limit_bytes and counter.bytes >= limit_bytes:
            stop.set()
            return
        url = random.choice(files)
        try:
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=socks_host,
                port=socks_port,
                rdns=True,
            )
            timeout = aiohttp.ClientTimeout(total=None, sock_read=30, sock_connect=15)
            async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
                async with sess.get(url) as resp:
                    if resp.status >= 400:
                        counter.fail(f"HTTP {resp.status} {url}")
                        await asyncio.sleep(0.5)
                        continue
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        counter.add(len(chunk))
                        if limit_bytes and counter.bytes >= limit_bytes:
                            stop.set()
                            return
                        if stop.is_set():
                            return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            counter.fail(f"{type(e).__name__}: {e}")
            await asyncio.sleep(1)


def _parse_socks(socks_url: str) -> tuple:
    p = urlparse(socks_url)
    return (p.hostname or "127.0.0.1", p.port or 10808)


async def burn(
    socks_url: str,
    workers: int,
    limit_bytes: int,
    files: List[str],
    counter: Counter,
    stop: Optional[asyncio.Event] = None,
) -> None:
    stop = stop or asyncio.Event()
    host, port = _parse_socks(socks_url)
    tasks = [
        asyncio.create_task(_worker(i, counter, host, port, limit_bytes, files, stop))
        for i in range(workers)
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
