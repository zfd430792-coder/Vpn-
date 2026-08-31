# -*- coding: utf-8 -*-
"""Проверить ноды родным Xray, без конверсии в sing-box.

Панель отдаёт готовый Xray-конфиг, а Happ внутри использует Xray. Если у
sing-box несовместимость с этими нодами, родной клиент подключится там,
где sing-box отдаёт "reality verification failed". Скрипт берёт outbound
из подписки КАК ЕСТЬ и проверяет соединение.

    /opt/vpn-traffic-bot/.venv/bin/python tools/try_xray.py '<URL подписки>'
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.loader import happ_headers  # noqa: E402
from bot.outbound import from_xray_outbound  # noqa: E402
from bot.subscription import fetch_full, parse  # noqa: E402
from try_nodes import socks_connect, run_once  # noqa: E402

PORT = 10997
XRAY_DIR = "/tmp/xray-test"
XRAY = os.path.join(XRAY_DIR, "xray")
VERSIONS = ["25.3.6", "1.8.24"]


def ensure_xray():
    if os.path.isfile(XRAY) and os.access(XRAY, os.X_OK):
        return True
    found = shutil.which("xray")
    if found:
        globals()["XRAY"] = found
        return True
    os.makedirs(XRAY_DIR, exist_ok=True)
    zp = os.path.join(XRAY_DIR, "x.zip")
    for v in VERSIONS:
        url = f"https://github.com/XTLS/Xray-core/releases/download/v{v}/Xray-linux-64.zip"
        print(f"  качаю Xray v{v}…")
        r = subprocess.run(["curl", "-fsSL", "--max-time", "120", url, "-o", zp])
        if r.returncode != 0:
            continue
        import zipfile
        try:
            zipfile.ZipFile(zp).extractall(XRAY_DIR)
        except Exception as e:
            print(f"  распаковка не удалась: {e}")
            continue
        os.chmod(XRAY, 0o755)
        return True
    return False


def xray_try(raw_ob, timeout=15):
    """Поднять Xray с сырым outbound из подписки и проверить туннель."""
    ob = json.loads(json.dumps(raw_ob))
    ob["tag"] = "proxy"
    cfg = {
        "log": {"loglevel": "warning"},
        "inbounds": [{"tag": "in", "port": PORT, "listen": "127.0.0.1",
                      "protocol": "socks",
                      "settings": {"auth": "noauth", "udp": False}}],
        "outbounds": [ob],
    }
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w") as f:
        json.dump(cfg, f)
    log = open("/tmp/try-xray.log", "a")
    proc = subprocess.Popen([XRAY, "run", "-c", path], stdout=log,
                            stderr=subprocess.STDOUT)
    try:
        import socket as sk
        for _ in range(50):
            try:
                sk.create_connection(("127.0.0.1", PORT), timeout=0.4).close()
                break
            except OSError:
                if proc.poll() is not None:
                    return False, "xray упал при старте"
                time.sleep(0.2)
        else:
            return False, "порт не поднялся"
        return socks_connect(PORT, "speedtest.tele2.net", 80, timeout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        os.unlink(path)
        log.close()


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки")
        return 1
    url = sys.argv[1]
    hwid = os.environ.get("SUB_HWID") or None

    print("=== Xray ===")
    if not ensure_xray():
        print("не удалось получить Xray — проверь доступ к github с сервера")
        return 1
    ver = subprocess.run([XRAY, "version"], capture_output=True, text=True).stdout
    print(f"  {ver.splitlines()[0] if ver else XRAY}\n")

    body, _ = fetch_full(url, ua="Happ/1.11.1", timeout=25, headers=happ_headers(hwid))
    parsed = parse(body)
    if parsed.get("kind") != "xray":
        print(f"формат {parsed.get('kind')}, а нужен Xray-конфиг от Happ")
        return 1

    raws = []
    for item in parsed["configs"]:
        for ob in item.get("outbounds") or []:
            if isinstance(ob, dict) and str(ob.get("protocol") or "").lower() in (
                    "vless", "vmess", "trojan", "shadowsocks"):
                raws.append((str(item.get("remarks") or ""), ob))
    print(f"нод в подписке: {len(raws)}, проверяю первые 3\n")

    sblog = open("/tmp/try-xray-sb.log", "w")
    win = []
    try:
        for remarks, raw in raws[:3]:
            vnext = (raw.get("settings") or {}).get("vnext") or [{}]
            addr = f"{vnext[0].get('address')}:{vnext[0].get('port')}"
            print(f"═══ {addr}  {remarks[:34]} ═══")

            ok_x, why_x = xray_try(raw)
            print(f"  Xray     (сырой конфиг)   → {'✅ РАБОТАЕТ' if ok_x else '  '} {why_x}")

            conv = from_xray_outbound(raw, remarks)
            if conv:
                fp = ((conv.get("tls") or {}).get("utls") or {}).get("fingerprint", "chrome")
                ok_s, why_s = run_once(conv, fp, conv.get("flow", ""), sblog)
                print(f"  sing-box (конверсия бота) → {'✅ РАБОТАЕТ' if ok_s else '  '} {why_s}")
            print()
            if ok_x:
                win.append(addr)
    finally:
        sblog.close()

    print("═" * 58)
    if win:
        print("✅ XRAY ПОДКЛЮЧАЕТСЯ, а sing-box нет:")
        for a in win:
            print(f"   {a}")
        print("\n   Значит подписка живая, а несовместим именно sing-box.")
        print("   Решение — гонять такие ноды через Xray.")
    else:
        print("❌ Ни Xray, ни sing-box не подключились.")
        print("   Родной клиент тоже не смог — ключи к нодам не подходят.")
        print("   Логи: /tmp/try-xray.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
