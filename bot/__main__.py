import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from .loader import HAPP_UA, fetch_and_load, outbounds_from_body
from .report import fmt_bytes, plan_summary, progress, units_to_bytes
from .singbox import SingBox, build_config
from .traffic import BIG_FILES, Counter, burn


async def run(args: argparse.Namespace) -> int:
    info: dict = {}
    ua_used = None
    if args.sub_file:
        body = Path(args.sub_file).read_text()
        outbounds = outbounds_from_body(body)
    else:
        outbounds, ua_used, raw, info = fetch_and_load(args.sub, ua=args.ua, hwid=args.hwid)
        if not outbounds and raw:
            print(
                f"подписка отдаёт только заглушки (узлов-пустышек: {len(raw)}). Проверь HWID.",
                file=sys.stderr,
            )

    if args.dry_run:
        print(json.dumps(outbounds, indent=2, ensure_ascii=False))
        if ua_used:
            print(f"# UA: {ua_used}, реальных нод: {len(outbounds)}", file=sys.stderr)
        if info:
            total, used, remaining = plan_summary(info)
            print(
                f"# план: использовано {fmt_bytes(used)} / {fmt_bytes(total)}, осталось {fmt_bytes(remaining)}",
                file=sys.stderr,
            )
        return 0
    if not outbounds:
        print("no usable outbounds found in subscription", file=sys.stderr)
        return 1
    if ua_used:
        print(f"UA: {ua_used}, реальных нод: {len(outbounds)}", file=sys.stderr)

    files = BIG_FILES
    if args.files:
        files = [
            line.strip()
            for line in Path(args.files).read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]

    config = build_config(outbounds, socks_port=args.port, log_level=args.log_level)
    limit_bytes = units_to_bytes(args.limit)
    if limit_bytes <= 0 and info:
        _, _, remaining = plan_summary(info)
        if remaining > 0:
            limit_bytes = remaining
            print(f"лимит = остаток плана: {fmt_bytes(remaining)}", file=sys.stderr)

    box = SingBox(binary=args.singbox)
    box.start(config, socks_port=args.port)
    try:
        counter = Counter()
        stop = asyncio.Event()
        report_task = asyncio.create_task(progress(lambda: counter.bytes, args.interval, limit_bytes, stop))
        burn_task = asyncio.create_task(
            burn("127.0.0.1", args.port, len(outbounds), args.workers, limit_bytes, files, counter, stop)
        )
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
    p.add_argument("--port", type=int, default=10808, help="базовый локальный SOCKS порт (дальше +1 на каждую ноду)")
    p.add_argument("--workers", type=int, default=32, help="количество параллельных загрузок")
    p.add_argument("--limit", default="0", help="остановиться после N (100GB / 0 = остаток плана или без лимита)")
    p.add_argument("--interval", type=float, default=5.0, help="секунд между строками прогресса")
    p.add_argument("--files", default="", help="файл со списком URL для качания (по строке)")
    p.add_argument("--ua", default=os.environ.get("SUB_UA") or HAPP_UA,
                   help="User-Agent, который пробуется первым (дальше перебор)")
    p.add_argument("--hwid", default=os.environ.get("SUB_HWID", ""),
                   help="HWID устройства для Happ (заголовок x-hwid)")
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
