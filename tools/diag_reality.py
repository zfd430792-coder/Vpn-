# -*- coding: utf-8 -*-
"""Диагностика REALITY-нод: что панель прислала и что из этого собрал бот.

Запуск на сервере с ботом:
    /opt/vpn-traffic-bot/.venv/bin/python /opt/vpn-traffic-bot/tools/diag_reality.py '<URL подписки>'

Секреты (uuid, public_key) маскируются — вывод можно пересылать.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.loader import USER_AGENTS, happ_headers  # noqa: E402
from bot.outbound import from_xray_outbound  # noqa: E402
from bot.subscription import fetch_full, parse  # noqa: E402


def mask(v):
    s = str(v or "")
    return f"<пусто>" if not s else f"{s[:4]}…{s[-2:]} (длина {len(s)})"


def check_pubkey(pk):
    """x25519 public key = 32 байта в base64url без паддинга = 43 символа."""
    s = str(pk or "")
    if not s:
        return "❌ ПУСТОЙ — reality работать не может"
    if len(s) != 43:
        return f"❌ длина {len(s)}, а должна быть 43 (32 байта base64url)"
    import base64
    try:
        raw = base64.urlsafe_b64decode(s + "=")
        if len(raw) != 32:
            return f"❌ декодируется в {len(raw)} байт вместо 32"
    except Exception as e:
        return f"❌ не декодируется: {e}"
    return "✅ формат верный"


def check_shortid(sid):
    s = str(sid if sid is not None else "")
    if s == "":
        return "⚠ пустой (валидно, только если сервер это разрешает)"
    if len(s) > 16:
        return f"❌ длина {len(s)} > 16"
    if len(s) % 2:
        return f"❌ нечётная длина {len(s)} — не hex-пара"
    if any(c not in "0123456789abcdefABCDEF" for c in s):
        return "❌ не hex"
    return "✅ формат верный"


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки первым аргументом")
        return 1
    url = sys.argv[1]
    hwid = os.environ.get("SUB_HWID", "") or None

    body = None
    for ua in ["Happ/1.11.1"] + USER_AGENTS:
        try:
            body, headers = fetch_full(url, ua=ua, timeout=25, headers=happ_headers(hwid))
        except Exception as e:
            print(f"UA {ua}: ошибка {type(e).__name__}: {e}")
            continue
        if body and body.strip():
            print(f"UA {ua}: получено {len(body)} байт")
            break
    if not body:
        print("подписка не отдала тело")
        return 1

    parsed = parse(body)
    print(f"формат подписки: {parsed['kind']}\n")
    if parsed["kind"] != "xray":
        print("это не Happ/Xray-массив — покажи вывод, разберу отдельно")
        return 0

    n = 0
    problems = []
    for item in parsed["configs"]:
        remarks = str(item.get("remarks") or "")
        for ob in item.get("outbounds") or []:
            if not isinstance(ob, dict):
                continue
            ss = ob.get("streamSettings") or {}
            sec = str(ss.get("security") or "none").lower()
            if sec != "reality":
                continue
            n += 1
            if n > 3:
                continue
            rs = ss.get("realitySettings") or {}
            vnext = (ob.get("settings") or {}).get("vnext") or [{}]
            addr = vnext[0].get("address", "?")
            conv = from_xray_outbound(ob, remarks) or {}
            ctls = conv.get("tls") or {}

            print(f"───── нода {n}: {remarks[:40]} ─────")
            print(f"  адрес ноды      : {addr}:{vnext[0].get('port')}")
            print(f"  ---- ЧТО ПРИСЛАЛА ПАНЕЛЬ ----")
            print(f"  serverName (SNI): {rs.get('serverName') or '<нет>'}")
            print(f"  publicKey       : {mask(rs.get('publicKey'))}  {check_pubkey(rs.get('publicKey'))}")
            print(f"  shortId         : {rs.get('shortId')!r}  {check_shortid(rs.get('shortId'))}")
            print(f"  fingerprint     : {rs.get('fingerprint') or '<нет — бот подставит chrome>'}")
            print(f"  flow            : {(vnext[0].get('users') or [{}])[0].get('flow') or '<нет>'}")
            print(f"  network         : {ss.get('network')}")
            print(f"  ---- ЧТО СОБРАЛ БОТ ----")
            print(f"  tls.server_name : {ctls.get('server_name')}")
            print(f"  tls.utls        : {ctls.get('utls')}")
            print(f"  tls.reality     : short_id={(ctls.get('reality') or {}).get('short_id')!r} "
                  f"public_key={mask((ctls.get('reality') or {}).get('public_key'))}")
            print(f"  flow            : {conv.get('flow') or '<нет>'}")

            if ctls.get("server_name") == addr:
                problems.append(f"нода {n}: SNI равен адресу ноды — для REALITY это провал верификации")
            if not (rs.get("publicKey") or ""):
                problems.append(f"нода {n}: панель прислала пустой publicKey")
            print()

    print(f"всего reality-нод в подписке: {n}")
    if problems:
        print("\n НАЙДЕННЫЕ ПРОБЛЕМЫ:")
        for p in problems:
            print("  ❌", p)
    else:
        print("\n Явных дефектов в параметрах не видно — причина глубже, пришли вывод целиком.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
