# -*- coding: utf-8 -*-
"""Почему конкретная нода не отвечает: она сама или наш конфиг.

Проверяет по шагам:
  1. TCP до ноды НАПРЯМУЮ, без sing-box — доступна ли она с этого сервера;
  2. TLS-рукопожатие на её порт — отвечает ли там вообще что-то;
  3. запрос через sing-box только с этой нодой — виноват ли наш конфиг.

    /opt/vpn-traffic-bot/.venv/bin/python tools/check_node.py '<URL подписки>' [строка-фильтр]
"""
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.loader import fetch_and_load  # noqa: E402
from bot.singbox import build_config, singbox_version  # noqa: E402
from try_nodes import socks_connect  # noqa: E402

SB = os.environ.get("SINGBOX_BIN", "/usr/local/bin/sing-box")
PORT = 10996


def tcp_check(host, port, timeout=8):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, f"открыт за {time.monotonic() - t0:.1f}с"
    except Exception as e:
        return False, f"{type(e).__name__}: {e} (за {time.monotonic() - t0:.1f}с)"


def tls_check(host, port, sni, timeout=8):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=sni or host) as ts:
                return True, f"{ts.version()}, сертификат {len(ts.getpeercert(True) or b'')} байт"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def via_singbox(ob, timeout=20):
    cfg = build_config([ob], socks_port=PORT, binary=SB)
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    log = open("/tmp/check-node.log", "w")
    proc = subprocess.Popen([SB, "run", "-c", path], stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.4).close()
                break
            except OSError:
                if proc.poll() is not None:
                    return False, "sing-box упал при старте", path
                time.sleep(0.2)
        else:
            return False, "порт sing-box не поднялся", path
        ok, why = socks_connect(PORT, "speedtest.tele2.net", 80, timeout)
        return ok, why, path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки; вторым аргументом — часть имени ноды")
        return 1
    url, needle = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "").lower()
    hwid = os.environ.get("SUB_HWID") or None
    print(f"sing-box: {SB} версии {singbox_version(SB)}\n")

    obs, ua, raw, info = fetch_and_load(url, ua=os.environ.get("SUB_UA") or None, hwid=hwid)
    if needle:
        obs = [o for o in obs if needle in str(o.get("tag", "")).lower()] or obs
    print(f"нод к проверке: {len(obs)} (UA {ua})\n")

    for ob in obs[:3]:
        host, port = ob.get("server"), ob.get("server_port")
        tls = ob.get("tls") or {}
        sni = tls.get("server_name")
        print(f"═══ {ob.get('tag')} ═══")
        print(f"  адрес   : {host}:{port}   тип {ob.get('type')}")
        print(f"  TLS     : {'reality' if (tls.get('reality') or {}).get('enabled') else ('tls' if tls.get('enabled') else 'нет')}"
              f"   SNI {sni}   flow {ob.get('flow') or '—'}")

        ok, why = tcp_check(host, port)
        print(f"  1) TCP напрямую      : {'✅' if ok else '❌'} {why}")
        if not ok:
            print("     ⇒ до ноды не достучаться С ЭТОГО СЕРВЕРА. sing-box и бот ни при чём:")
            print("       блокирует файрвол сервера, хостер или сама нода режет этот IP.")
            print()
            continue

        ok2, why2 = tls_check(host, port, sni)
        print(f"  2) TLS на её порт    : {'✅' if ok2 else '❌'} {why2}")

        ok3, why3, cfgpath = via_singbox(ob)
        print(f"  3) через sing-box    : {'✅' if ok3 else '❌'} {why3}")
        if not ok3:
            print(f"     конфиг ноды: {cfgpath}")
            try:
                tail = open("/tmp/check-node.log").read().strip().splitlines()[-4:]
                for t in tail:
                    print(f"     log: {t[:150]}")
            except OSError:
                pass
            print("     ⇒ TCP есть, а через sing-box не идёт — вот это уже наш конфиг.")
        else:
            print("     ⇒ нода полностью рабочая, и бот через неё ходит.")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
