# -*- coding: utf-8 -*-
"""Полная структура подписки: всё, что панель прислала, и что бот из этого взял.

Ищем то, что могло ускользнуть: ноды других протоколов, цепочки dialerProxy,
fragment/sockopt, разные наборы для разных User-Agent.

    /opt/vpn-traffic-bot/.venv/bin/python tools/dump_sub.py '<URL подписки>'
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.loader import USER_AGENTS, happ_headers, is_dummy  # noqa: E402
from bot.outbound import from_xray_outbound  # noqa: E402
from bot.subscription import fetch_full, parse  # noqa: E402

SECRET_KEYS = {"id", "password", "publicKey", "uuid", "public_key", "privateKey"}


def redact(obj):
    """Копия структуры с замазанными секретами."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SECRET_KEYS and isinstance(v, str) and v:
                out[k] = f"<{len(v)} симв: {v[:4]}…>"
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки")
        return 1
    url = sys.argv[1]
    hwid = os.environ.get("SUB_HWID") or None

    # ---- 1. что отдаёт панель разным клиентам ----
    print("===== ОТВЕТ ПАНЕЛИ РАЗНЫМ КЛИЕНТАМ =====")
    bodies = {}
    for ua in ["Happ/1.11.1", "v2rayN/6.42", "sing-box/1.11.0", "clash-verge/1.7.7",
               "Shadowrocket/2.2.9"]:
        try:
            body, hdrs = fetch_full(url, ua=ua, timeout=25, headers=happ_headers(hwid))
        except Exception as e:
            print(f"  {ua:22} → ошибка {type(e).__name__}")
            continue
        try:
            kind = parse(body).get("kind")
        except Exception:
            kind = "?"
        bodies[ua] = body
        same = ""
        for prev_ua, prev in bodies.items():
            if prev_ua != ua and prev == body:
                same = f"  (идентично {prev_ua})"
                break
        print(f"  {ua:22} → {len(body):>6} байт, формат {kind}{same}")

    body = bodies.get("Happ/1.11.1") or next(iter(bodies.values()), None)
    if not body:
        print("панель ничего не отдала")
        return 1

    parsed = parse(body)
    if parsed.get("kind") != "xray":
        print(f"\nформат {parsed.get('kind')} — дальше разбор рассчитан на Xray")
        return 0

    # ---- 2. полная опись того, что внутри ----
    print(f"\n===== ЧТО ВНУТРИ (конфигов в массиве: {len(parsed['configs'])}) =====")
    protos, streams, extras = {}, {}, {}
    total_ob = 0
    converted = 0
    skipped = []
    for ci, item in enumerate(parsed["configs"]):
        obs = item.get("outbounds") or []
        for ob in obs:
            if not isinstance(ob, dict):
                continue
            total_ob += 1
            p = str(ob.get("protocol") or "?")
            protos[p] = protos.get(p, 0) + 1
            ss = ob.get("streamSettings") or {}
            key = f"{ss.get('network') or '-'}/{ss.get('security') or 'none'}"
            streams[key] = streams.get(key, 0) + 1
            # поля, которые бот НЕ переносит
            for extra in ("sockopt", "mux", "fragment", "noises"):
                if ss.get(extra) or ob.get(extra):
                    extras[extra] = extras.get(extra, 0) + 1
            if (ss.get("sockopt") or {}).get("dialerProxy"):
                extras["dialerProxy"] = extras.get("dialerProxy", 0) + 1
            conv = from_xray_outbound(ob, str(item.get("remarks") or ""))
            if conv and not is_dummy(conv):
                converted += 1
            else:
                skipped.append(f"конфиг {ci}: protocol={p} network={ss.get('network')}")

    print(f"  outbound'ов всего      : {total_ob}")
    print(f"  бот сконвертировал     : {converted}")
    print(f"  бот пропустил          : {len(skipped)}")
    print(f"  протоколы              : {protos}")
    print(f"  transport/security     : {streams}")
    print(f"  поля вне разбора бота  : {extras or 'нет'}")
    if skipped:
        print("  что именно пропущено:")
        for s in skipped[:10]:
            print(f"    - {s}")

    # ---- 3. сырой outbound целиком ----
    print("\n===== СЫРОЙ OUTBOUND ЦЕЛИКОМ (секреты замазаны) =====")
    shown = 0
    for item in parsed["configs"]:
        for ob in item.get("outbounds") or []:
            if not isinstance(ob, dict):
                continue
            print(json.dumps(redact(ob), indent=2, ensure_ascii=False)[:2600])
            shown += 1
            break
        if shown:
            break

    # ---- 4. верхний уровень конфига ----
    print("\n===== КЛЮЧИ ВЕРХНЕГО УРОВНЯ ПЕРВОГО КОНФИГА =====")
    first = parsed["configs"][0]
    for k, v in first.items():
        if k == "outbounds":
            print(f"  {k}: [{len(v)} шт]")
        else:
            print(f"  {k}: {json.dumps(redact(v), ensure_ascii=False)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
