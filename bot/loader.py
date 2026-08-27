from typing import List, Optional, Tuple

from .outbound import from_clash_proxies, from_uris
from .subscription import fetch, parse


# Панели часто отдают реальные ноды только "своему" клиенту и определяют
# его по User-Agent. Перебираем популярные клиенты, пока не придут
# настоящие узлы (Happ первым — многие панели требуют именно его).
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


def is_dummy(ob: dict) -> bool:
    """Заглушка вида server=0.0.0.0, port=1, нулевой uuid и т.п."""
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
) -> Tuple[List[dict], str, int]:
    """Перебирает User-Agent'ы, пока подписка не отдаст реальные ноды.

    Возвращает (outbounds, ua_used, raw_seen) — где raw_seen это сколько
    всего узлов (включая заглушки) отдавала лучшая попытка.
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

    last_err: Optional[Exception] = None
    best_raw = 0
    best_ua = ordered[0] if ordered else ""
    for u in ordered:
        try:
            body = fetch(url, ua=u, timeout=timeout)
            raw = _raw_outbounds(body)
        except Exception as e:  # noqa: BLE001 - пробуем следующий UA
            last_err = e
            continue
        real = [o for o in raw if not is_dummy(o)]
        if len(raw) > best_raw:
            best_raw = len(raw)
            best_ua = u
        if real:
            return real, u, len(raw)

    if best_raw == 0 and last_err is not None:
        raise last_err
    return [], best_ua, best_raw
