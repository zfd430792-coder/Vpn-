# -*- coding: utf-8 -*-
"""Перебор вариантов подключения к нодам подписки.

REALITY-верификация зависит не только от ключей, но и от uTLS-профиля
(fingerprint) и от flow. Скрипт поднимает sing-box с одной нодой за раз,
перебирает комбинации и проверяет, устанавливается ли соединение.

Проверка — SOCKS5 CONNECT через локальный порт: если sing-box отвечает
success, туннель до ноды поднялся. Никаких зависимостей, чистый socket.

    /opt/vpn-traffic-bot/.venv/bin/python tools/try_nodes.py '<URL подписки>'
"""
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.loader import happ_headers  # noqa: E402
from bot.outbound import from_xray_outbound  # noqa: E402
from bot.subscription import fetch_full, parse  # noqa: E402

FINGERPRINTS = ["firefox", "chrome", "safari", "ios", "edge", "random", "randomized"]
FLOWS = ["", "xtls-rprx-vision"]
SB = os.environ.get("SINGBOX_BIN", "/usr/local/bin/sing-box")
PORT = 10999


def socks_connect(port, host, dport, timeout=12, fetch=True):
    """SOCKS5 CONNECT и реальная прокачка данных.

    Одного ответа прокси мало: Xray отвечает success сразу, а соединение с
    целью устанавливает лениво, поэтому мёртвая нода выглядела бы живой.
    Проверяем настоящим HTTP-запросом через туннель.
    """
    codes = {0: "success", 1: "general SOCKS server failure", 2: "not allowed",
             3: "network unreachable", 4: "host unreachable",
             5: "connection refused", 6: "TTL expired"}
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError as e:
        return False, f"порт не открылся: {e}"
    try:
        s.settimeout(timeout)
        s.sendall(b"\x05\x01\x00")
        if s.recv(2)[:1] != b"\x05":
            return False, "не SOCKS5"
        hb = host.encode()
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", dport))
        resp = s.recv(4)
        if len(resp) < 2:
            return False, "пустой ответ"
        code = resp[1]
        if code != 0:
            return False, codes.get(code, f"код {code}")
        if not fetch:
            return True, "connect ok"
        # дочитываем адрес привязки, чтобы не съесть его вместе с данными
        atyp = resp[3] if len(resp) > 3 else 1
        if atyp == 1:
            s.recv(4 + 2)
        elif atyp == 3:
            ln = s.recv(1)
            s.recv((ln[0] if ln else 0) + 2)
        elif atyp == 4:
            s.recv(16 + 2)
        s.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\n"
                  f"User-Agent: curl/8\r\nConnection: close\r\n\r\n".encode())
        data = s.recv(64)
        if not data:
            return False, "туннель открылся, но данные не идут"
        if not data.startswith(b"HTTP/"):
            return False, f"мусор вместо ответа: {data[:24]!r}"
        return True, f"прокачка ок ({data.split(chr(13).encode())[0].decode(errors='replace')[:24]})"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"
    finally:
        s.close()


def run_once(ob, fp, flow, logf):
    """Поднять sing-box с одной нодой и проверить туннель."""
    o = json.loads(json.dumps(ob))
    tls = o.setdefault("tls", {})
    if tls.get("reality"):
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    if flow:
        o["flow"] = flow
    else:
        o.pop("flow", None)
    cfg = {
        "log": {"level": "error"},
        "inbounds": [{"type": "mixed", "tag": "in", "listen": "127.0.0.1",
                      "listen_port": PORT}],
        "outbounds": [o, {"type": "direct", "tag": "direct"}],
        "route": {"rules": [{"inbound": ["in"], "outbound": o["tag"]}]},
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        json.dump(cfg, f)
    proc = subprocess.Popen([SB, "run", "-c", path], stdout=logf,
                            stderr=subprocess.STDOUT)
    try:
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", PORT), timeout=0.4).close()
                break
            except OSError:
                if proc.poll() is not None:
                    return False, "sing-box упал при старте"
                time.sleep(0.2)
        else:
            return False, "порт не поднялся"
        return socks_connect(PORT, "speedtest.tele2.net", 80)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(path)


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки")
        return 1
    url = sys.argv[1]
    hwid = os.environ.get("SUB_HWID") or None
    if not (os.path.isfile(SB) or __import__("shutil").which(SB)):
        print(f"не найден sing-box по пути {SB} — задай SINGBOX_BIN")
        return 1

    body, _ = fetch_full(url, ua="Happ/1.11.1", timeout=25, headers=happ_headers(hwid))
    parsed = parse(body)
    if parsed.get("kind") != "xray":
        print(f"формат {parsed.get('kind')} — скрипт рассчитан на Happ/Xray")
        return 1

    nodes = []
    for item in parsed["configs"]:
        for ob in item.get("outbounds") or []:
            if not isinstance(ob, dict):
                continue
            if str((ob.get("streamSettings") or {}).get("security") or "").lower() != "reality":
                continue
            conv = from_xray_outbound(ob, str(item.get("remarks") or ""))
            if conv:
                nodes.append(conv)
    print(f"reality-нод: {len(nodes)}, беру первые 3\n")

    logf = open("/tmp/try-nodes.log", "w")
    found = []
    try:
        for ob in nodes[:3]:
            print(f"═══ {ob.get('server')}:{ob.get('server_port')} "
                  f"(SNI {(ob.get('tls') or {}).get('server_name')}) ═══")
            for flow in FLOWS:
                for fp in FINGERPRINTS:
                    ok, why = run_once(ob, fp, flow, logf)
                    mark = "✅" if ok else "  "
                    fl = flow or "нет"
                    print(f"  {mark} fp={fp:<11} flow={fl:<17} → {why}")
                    if ok:
                        found.append((ob.get("server"), fp, flow))
                        break
                if found and found[-1][0] == ob.get("server"):
                    break
            print()
    finally:
        logf.close()

    print("═" * 58)
    if found:
        print("✅ РАБОЧАЯ КОМБИНАЦИЯ НАЙДЕНА:")
        for srv, fp, flow in found:
            print(f"   {srv}: fingerprint={fp}, flow={flow or 'нет'}")
    else:
        print("❌ Ни одна комбинация не подошла.")
        print("   Значит ключи из подписки к этим нодам действительно не подходят —")
        print("   панель отдаёт нерабочий конфиг всем, кроме своего приложения.")
        print("   Подробности: /tmp/try-nodes.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
