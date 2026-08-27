import asyncio
import time
from typing import Callable


def fmt_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024:
            return f"{value:6.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PiB"


async def progress(get_bytes: Callable[[], int], interval: float, limit_bytes: int, stop: asyncio.Event) -> None:
    prev = get_bytes()
    prev_t = time.monotonic()
    started = prev_t
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass
        now = get_bytes()
        t = time.monotonic()
        rate = (now - prev) / max(t - prev_t, 1e-6)
        avg = now / max(t - started, 1e-6)
        prev, prev_t = now, t
        goal = f" / {fmt_bytes(limit_bytes)}" if limit_bytes else ""
        print(
            f"[{int(t - started):5d}s] eaten {fmt_bytes(now)}{goal}  now {fmt_bytes(rate)}/s  avg {fmt_bytes(avg)}/s",
            flush=True,
        )
        if limit_bytes and now >= limit_bytes:
            stop.set()
            return
