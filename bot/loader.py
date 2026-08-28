from typing import Dict, List, Optional, Tuple

from .outbound import from_clash_proxies, from_uris
from .subscription import fetch_full, parse


# Панели часто отдают реальные ноды только "своему" клиенту (по User-Agent).
USER_AGENTS: List[str] = [
    "Happ/1.11.1",
    "Happ",
    "v2rayNG/1.9.5",
    "sing-box/1.11.0",
    "SFA/1.11.0",
    "Streisand",
    "Shadowrocket/2.2.9",
    "clash-verge/1.7.7",
    "NekoBox/1.3.5",
    "clash.meta",
    "v2rayN/6.42",
]

_DUMMY_SERVERS = {"", "0.0.0.0", "0", "127.0.0.1", "::", "::1", "localhost"}


def happ_headers(hwid: Optional[str]) -> Dict[str, str]:
    if not hwid:
        return {}
    return {
        "x-hwid": hwid,
        "x-device-os": "Android",
        "x-ver-os": "14",
        "x-device-model": "Samsung SM-S918B",
    }


def parse_userinfo(headers: Dict[str, str]) -> Dict[str, int]:
    raw = ""
    for k, v in headers.items():
        if k.lower() == "subscription-userinfo":
            raw = v
            break
    info: Dict[str, int] = {}
    if not raw:
        return info
    for part in raw.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        try:
            info[key.strip().lower()] = int(val.strip())
        except ValueError:
            pass
    return info


def is_dummy(ob: dict) -> bool:
    server = str(ob.get("server", "")).strip().lower()
    if server in _DUMMY_SERVERS:
        return True
    try:
        if int(ob.get("server_port") or 0) <= 1:
            return True
    except (TypeError, ValueError):
        return True
    uuid = str(ob.get("uuid", "")).strip()
    if uuid and uuid.replace("0", "").replace("-", "") == "":
        return True
    if ob.get("type") in ("trojan", "shadowsocks", "hysteria2"):
        if not str(ob.get("password", "")).strip():
            return True
    return False


def _raw_outbounds(body: str) -> List[dict]:
    parsed = parse(body)
    kind = parsed["kind"]
    if kind == "uris":
        return from_uris(parsed["uris"])
    if kind == "clash":
        return from_clash_proxies(parsed["proxies"])
    skip = {"selector", "urltest", "direct", "block", "dns"}
    return [o for o in parsed["config"].get("outbounds", []) if o.get("type") not in skip]


def outbounds_from_body(body: str) -> List[dict]:
    return [o for o in _raw_outbounds(body) if not is_dummy(o)]


def fetch_and_load(
    url: str,
    ua: Optional[str] = None,
    uas: Optional[List[str]] = None,
    timeout: int = 20,
    hwid: Optional[str] = None,
) -> Tuple[List[dict], str, List[dict], Dict[str, int]]:
    """Перебирает UA (с Happ-заголовками, если HWID), пока не придут реальные ноды.

    Возвращает (real_outbounds, ua_used, raw_outbounds, userinfo).
    raw_outbounds — всё что отдала подписка (включая заглушки с их тегами).
    """
    candidates: List[str] = []
    if ua:
        candidates.append(ua)
    candidates.extend(uas or USER_AGENTS)

    seen = set()
    ordered: List[str] = []
    for u in candidates:
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    if hwid:
        ordered.sort(key=lambda u: 0 if "happ" in u.lower() else 1)

    extra = happ_headers(hwid)
    last_err: Optional[Exception] = None
    best_raw: List[dict] = []
    best_ua = ordered[0] if ordered else ""
    best_info: Dict[str, int] = {}
    for u in ordered:
        try:
            body, resp_headers = fetch_full(url, ua=u, timeout=timeout, headers=extra)
            raw = _raw_outbounds(body)
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        info = parse_userinfo(resp_headers)
        real = [o for o in raw if not is_dummy(o)]
        if len(raw) > len(best_raw):
            best_raw = raw
            best_ua = u
            best_info = info
        if real:
            return real, u, raw, info

    if not best_raw and last_err is not None:
        raise last_err
    return [], best_ua, best_raw, best_info
