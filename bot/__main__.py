import argparse
import asyncio
import json
import sys
from pathlib import Path

from .outbound import from_clash_proxies, from_uris
from .report import progress
from .singbox import SingBox, build_config
from .subscription import fetch, parse
from .traffic import BIG_FILES, Counter, burn


UNIT_SUFFIXES = [
    ("tb", 1024 ** 4), ("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024),
    ("t", 1024 ** 4), ("g", 1024 ** 3), ("m", 1024 ** 2), ("k", 1024),
    ("b", 1),
]


def units_to_bytes(s: str) -> int:
    s = (s or "").strip().lower()
    if not s or s in ("0", "none", "unlimited", "inf"):
        return 0
    for suffix, mult in UNIT_SUFFIXES:
        if s.endswith(suffix):
            return int(float(s[: -len(suffix)]) * mult)
    return int(float(s))


def load_outbounds(sub_body: str) -> list:
    parsed = parse(sub_body)
    kind = parsed["kind"]
    if kind == "uris":
        return from_uris(parsed["uris"])
    if kind == "clash":
        return from_clash_proxies(parsed["proxies"])
    obs = parsed["config"].get("outbounds", [])
    skip = {"selector", "urltest", "direct", "block", "dns"}
    return [o for o in obs if o.get("type") not in skip]


async def run(args: argparse.Namespace) -> int:
    if args.sub_file:
        body = Path(args.sub_file).read_text()
    else:
        body = fetch(args.sub, ua=args.ua)

    outbounds = load_outbounds(body)
    if args.dry_run:
        print(json.dumps(outbounds, indent=2, ensure_ascii=False))
        return 0
    if not outbounds:
        print("no usable outbounds found in subscription", file=sys.stderr)
        return 1

    files = BIG_FILES
    if args.files:
        files = [
            line.strip()
            for line in Path(args.files).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    config = build_config(outbounds, socks_port=args.port, log_level=args.log_level)
    limit_bytes = units_to_bytes(args.limit)

    box = SingBox(binary=args.singbox)
    box.start(config, socks_port=args.port)
    try:
        counter = Counter()
        stop = asyncio.Event()
        socks_url = f"socks5://127.0.0.1:{args.port}"
        report_task = asyncio.create_task(progress(lambda: counter.bytes, args.interval, limit_bytes, stop))
        burn_task = asyncio.create_task(burn(socks_url, args.workers, limit_bytes, files, counter, stop))
        try:
            await asyncio.gather(report_task, burn_task)
        except asyncio.CancelledError:
            stop.set()
            raise
    finally:
        box.stop()
    return 0


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="vpn-traffic-bot",
        description="Ест трафик подписки VPN через локальный sing-box.",
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--sub", help="URL подписки")
    src.add_argument("--sub-file", help="локальный файл с содержимым подписки")
    p.add_argument("--singbox", default="sing-box", help="путь к бинарнику sing-box")
    p.add_argument("--port", type=int, default=10808, help="локальный SOCKS/HTTP порт")
    p.add_argument("--workers", type=int, default=16, help="количество параллельных загрузок")
    p.add_argument("--limit", default="0", help="остановиться после N трафика (100GB, 500MB, 0 = без лимита)")
    p.add_argument("--interval", type=float, default=5.0, help="секунд между строками прогресса")
    p.add_argument("--files", default="", help="файл со списком URL для качания (по строке)")
    p.add_argument("--ua", default="v2rayN/6.42", help="User-Agent при запросе подписки")
    p.add_argument("--log-level", default="warn", help="уровень логирования sing-box")
    p.add_argument("--dry-run", action="store_true", help="распарсить подписку и вывести outbounds")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
