# -*- coding: utf-8 -*-
"""Диагностика REALITY-нод: что панель прислала и что из этого собрал бот.

Запуск на сервере с ботом:
    /opt/vpn-traffic-bot/.venv/bin/python /opt/vpn-traffic-bot/tools/diag_reality.py '<URL подписки>'

Секреты (uuid, public_key) маскируются — вывод можно пересылать.
"""
import json
import os
import re
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


def compare_with_link(link, sub_pbk, sub_sid):
    """Сверить ключи из подписки с рабочей ссылкой vless:// из приложения."""
    from bot.outbound import parse_uri
    ob = parse_uri(link.strip())
    if not ob:
        print("не разобрал ссылку — она должна начинаться с vless://")
        return
    tls = ob.get("tls") or {}
    r = tls.get("reality") or {}
    pbk, sid = r.get("public_key", ""), r.get("short_id", "")
    print("\n===== СВЕРКА С РАБОЧЕЙ ССЫЛКОЙ ИЗ ПРИЛОЖЕНИЯ =====")
    print(f"  сервер в ссылке : {ob.get('server')}:{ob.get('server_port')}")
    print(f"  SNI в ссылке    : {tls.get('server_name')}")
    print(f"  publicKey ссылки: {mask(pbk)}")
    print(f"  publicKey подписки: {mask(sub_pbk)}")
    print(f"  shortId ссылки  : {sid!r}")
    print(f"  shortId подписки: {sub_sid!r}")
    print(f"  flow            : {ob.get('flow') or '<нет>'}")
    same_pbk, same_sid = pbk == sub_pbk, sid == sub_sid
    print()
    if same_pbk and same_sid:
        print("  ✅ КЛЮЧИ СОВПАДАЮТ — панель отдаёт боту то же, что приложению.")
        print("     Значит дело не в ключах: смотри flow и transport выше.")
    else:
        if not same_pbk:
            print("  ❌ publicKey РАЗНЫЙ")
        if not same_sid:
            print("  ❌ shortId РАЗНЫЙ")
        print("     Панель отдаёт приложению одни ключи, а боту другие —")
        print("     это панель-стена. Обход: скорми боту саму ссылку vless://.")


def main():
    if len(sys.argv) < 2:
        print("нужен URL подписки первым аргументом")
        print("для сверки:  diag_reality.py '<URL>' 'vless://…ссылка из Happ'")
        return 1
    url = sys.argv[1]
    compare_link = sys.argv[2] if len(sys.argv) > 2 else ""
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
    first_keys = {"pbk": "", "sid": ""}
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

            if n == 1:
                first_keys["pbk"] = (ctls.get("reality") or {}).get("public_key", "")
                first_keys["sid"] = (ctls.get("reality") or {}).get("short_id", "")
            if ctls.get("server_name") == addr:
                problems.append(f"нода {n}: SNI равен адресу ноды — для REALITY это провал верификации")
            if not (rs.get("publicKey") or ""):
                problems.append(f"нода {n}: панель прислала пустой publicKey")
            print()

    print(f"всего reality-нод в подписке: {n}")
    if compare_link:
        compare_with_link(compare_link, first_keys["pbk"], first_keys["sid"])

    # ---- живость нод и чем они отвечают на 443 ----
    # REALITY-нода, не признавшая клиента, молча проксирует на маскировочный
    # домен и отдаёт ЕГО настоящий сертификат. Это отличает "ключи не подошли"
    # от "нода недоступна".
    import socket
    import ssl
    print("\n===== ПРОВЕРКА САМИХ НОД =====")
    checked = 0
    for item in parsed["configs"]:
        for ob in item.get("outbounds") or []:
            if not isinstance(ob, dict):
                continue
            ss = ob.get("streamSettings") or {}
            if str(ss.get("security") or "").lower() != "reality":
                continue
            vnext = (ob.get("settings") or {}).get("vnext") or [{}]
            host, port = vnext[0].get("address"), int(vnext[0].get("port") or 443)
            sni = (ss.get("realitySettings") or {}).get("serverName") or ""
            checked += 1
            if checked > 3:
                break
            print(f"\n─ {host}:{port}  (SNI {sni})")
            try:
                with socket.create_connection((host, port), timeout=8):
                    print("  TCP        : ✅ порт открыт")
            except Exception as e:
                print(f"  TCP        : ❌ {type(e).__name__}: {e}")
                continue
            try:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                with socket.create_connection((host, port), timeout=8) as s:
                    with ctx.wrap_socket(s, server_hostname=sni) as ts:
                        der = ts.getpeercert(binary_form=True) or b""
                        print(f"  TLS        : ✅ хендшейк прошёл, {ts.version()}")
                        print(f"  размер cert: {len(der)} байт")
                        # при CERT_NONE python не разбирает сертификат в словарь,
                        # поэтому имена достаём прямо из DER
                        names = sorted(set(
                            m.decode() for m in re.findall(
                                rb"[a-z0-9\*][a-z0-9\-\.\*]{3,}\.[a-z]{2,}", der.lower())
                        ))
                        shown = ", ".join(names[:6]) or "<имён не найдено>"
                        print(f"  домены cert: {shown}")
                        base = sni.split(".")[-2] if sni.count(".") >= 1 else sni
                        if base and any(base in nm for nm in names):
                            print("  ВЫВОД      : нода ЖИВА и ведёт себя как исправная")
                            print("               REALITY-нода — постороннему клиенту она")
                            print("               отдаёт сертификат маскировочного домена.")
                            print("               Проба шла БЕЗ ключей, поэтому о годности")
                            print("               ключей это ничего не говорит.")
                        else:
                            print("  ВЫВОД      : сертификат НЕ от маскировочного домена —")
                            print("               на этом IP отвечает не та нода (подмена/заглушка)")
            except Exception as e:
                print(f"  TLS        : ❌ {type(e).__name__}: {e}")
        if checked > 3:
            break
    if problems:
        print("\n НАЙДЕННЫЕ ПРОБЛЕМЫ:")
        for p in problems:
            print("  ❌", p)
    else:
        print("\n Явных дефектов в параметрах не видно — причина глубже, пришли вывод целиком.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
