import asyncio
import random
import time
from typing import List, Optional

import aiohttp
from aiohttp_socks import ProxyConnector, ProxyType


CHUNK = 1 << 20  # 1 MiB

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


async def _worker(
    idx: int,
    counter: Counter,
    socks_host: str,
    socks_port: int,
    limit_bytes: int,
    files: List[str],
    stop: asyncio.Event,
) -> None:
    timeout = aiohttp.ClientTimeout(total=None, sock_read=60, sock_connect=20)
    while not stop.is_set():
        if limit_bytes and counter.bytes >= limit_bytes:
            stop.set()
            return
        try:
            connector = ProxyConnector(
                proxy_type=ProxyType.SOCKS5,
                host=socks_host,
                port=socks_port,
                rdns=True,
            )
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
                    except Exception as e:  # noqa: BLE001 - пересобираем сессию
                        counter.fail(f"{type(e).__name__}: {e}")
                        break
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            counter.fail(f"{type(e).__name__}: {e}")
            await asyncio.sleep(0.5)


async def burn(
    socks_host: str,
    base_port: int,
    node_count: int,
    workers: int,
    limit_bytes: int,
    files: List[str],
    counter: Counter,
    stop: Optional[asyncio.Event] = None,
) -> None:
    stop = stop or asyncio.Event()
    node_count = max(int(node_count), 1)
    tasks = [
        asyncio.create_task(
            _worker(i, counter, socks_host, base_port + (i % node_count), limit_bytes, files, stop)
        )
        for i in range(workers)
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
