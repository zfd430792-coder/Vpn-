import asyncio
import time
from typing import Callable, Dict, Tuple


def fmt_bytes(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024:
            return f"{value:6.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PiB"


_UNIT_SUFFIXES = [
    ("tb", 1024 ** 4), ("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024),
    ("t", 1024 ** 4), ("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024),
    ("b", 1),
]


def units_to_bytes(s: str) -> int:
    s = (s or "").strip().lower()
    if not s or s in ("0", "none", "unlimited", "inf"):
        return 0
    for suffix, mult in _UNIT_SUFFIXES:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(float(s))


def plan_summary(info: Dict[str, int]) -> Tuple[int, int, int]:
    """(total, used, remaining) из Subscription-Userinfo. 0 если нет данных."""
    total = int(info.get("total") or 0)
    used = int(info.get("upload") or 0) + int(info.get("download") or 0)
    remaining = max(total - used, 0) if total else 0
    return total, used, remaining


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
