# -*- coding: utf-8 -*-
"""Какой User-Agent даёт РАБОЧИЕ ноды.

Панель может отдавать разным клиентам разные подписки. Скрипт для каждого
UA берёт ноды и проверяет их живым подключением через sing-box, а не по
формальным признакам.

    /opt/vpn-traffic-bot/.venv/bin/python tools/find_working_ua.py '<URL>'
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bot.loader import happ_headers, is_dummy, _raw_outbounds  # noqa: E402
from bot.subscription import fetch_full  # noqa: E402
from try_nodes import run_once  # noqa: E402

UAS = ["v2rayN/6.42", "sing-box/1.11.0", "clash-verge/1.7.7", "Shadowrocket/2.2.9",
       "Streisand", "NekoBox/1.3.5", "v2rayNG/1.9.5", "Happ/1.11.1"]


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки")
        return 1
    url = sys.argv[1]
    hwid = os.environ.get("SUB_HWID") or None
    logf = open("/tmp/find-ua.log", "w")
    winners = []
    try:
        for ua in UAS:
            try:
                body, _ = fetch_full(url, ua=ua, timeout=25, headers=happ_headers(hwid))
            except Exception as e:
                print(f"\n═══ {ua} ═══\n  запрос не прошёл: {type(e).__name__}")
                continue
            try:
                raw = _raw_outbounds(body)
            except Exception as e:
                print(f"\n═══ {ua} ═══\n  разбор упал: {e}")
                continue
            nodes = [o for o in raw if not is_dummy(o)]
            print(f"\n═══ {ua} ═══  {len(body)} байт, нод {len(nodes)}")
            if not nodes:
                print("  нод нет")
                continue
            tested = 0
            ok_any = False
            for ob in nodes:
                if tested >= 2:
                    break
                tested += 1
                tls = ob.get("tls") or {}
                kind = ob.get("type")
                sec = "reality" if (tls.get("reality") or {}).get("enabled") else (
                    "tls" if tls.get("enabled") else "none")
                fp = (tls.get("utls") or {}).get("fingerprint", "")
                ok, why = run_once(ob, fp or "chrome", ob.get("flow", ""), logf)
                mark = "✅ РАБОТАЕТ" if ok else "  "
                print(f"  {mark} {kind}/{sec} {ob.get('server')}:{ob.get('server_port')} → {why}")
                if ok:
                    ok_any = True
                    winners.append((ua, ob.get("server"), kind, sec))
                    break
            if ok_any:
                print(f"  ➜ у этого UA ноды рабочие")
    finally:
        logf.close()

    print("\n" + "═" * 58)
    if winners:
        print("✅ РАБОЧИЕ НОДЫ НАЙДЕНЫ:")
        for ua, srv, kind, sec in winners:
            print(f"   UA={ua}  →  {kind}/{sec}  {srv}")
        print("\n   Значит подписка годная, просто бот брал не тот вариант.")
    else:
        print("❌ Ни один UA не дал рабочих нод.")
        print("   Подробности: /tmp/find-ua.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
