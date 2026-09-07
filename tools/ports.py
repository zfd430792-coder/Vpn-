# -*- coding: utf-8 -*-
"""Что в подписке против того, что видит сервер.

Для каждой ноды печатает адрес и порт, взятые из подписки, и тут же
проверяет TCP до них НАПРЯМУЮ, без sing-box. Если подписка отдала одно, а
в конфиге другое — это наша ошибка. Если совпадает, а TCP не идёт — до
ноды не достучаться с этого сервера, и код ни при чём.

    /opt/vpn-traffic-bot/.venv/bin/python tools/ports.py '<URL подписки>'
"""
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.loader import fetch_and_load  # noqa: E402


def tcp(host, port, timeout=6.0):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, f"{(time.monotonic() - t0) * 1000:.0f} мс"
    except Exception as e:
        return False, f"{type(e).__name__} за {time.monotonic() - t0:.1f}с"


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки")
        return 1
    obs, ua, raw, info = fetch_and_load(
        sys.argv[1], ua=os.environ.get("SUB_UA") or None,
        hwid=os.environ.get("SUB_HWID") or None)
    print(f"нод в подписке: {len(obs)} (UA {ua})\n")
    alive, dead = [], []
    for ob in obs:
        host, port = ob.get("server"), ob.get("server_port")
        ok, why = tcp(host, port)
        tag = str(ob.get("tag", ""))[:34]
        print(f"  {'✅' if ok else '❌'} {tag:36} {host}:{port:<6} {why}")
        (alive if ok else dead).append((tag, host, port))
    print()
    print(f"живых по TCP: {len(alive)} из {len(obs)}")
    if alive:
        print("\nРабочие ноды — выбери такую страну в карточке ключа:")
        for tag, h, p in alive[:8]:
            print(f"   {tag}  ({h}:{p})")
    else:
        hosts = {h for _, h, _ in dead}
        print("\nНи одна нода не принимает TCP с этого сервера.")
        print(f"Адресов всего: {len(hosts)} → {', '.join(sorted(hosts))}")
        print("Адрес и порт взяты из подписки как есть, sing-box тут ещё не")
        print("участвует — значит режется путь сервер→нода, а не наш конфиг.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
