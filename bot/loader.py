import urllib.parse as up
from typing import Dict, List, Optional, Tuple

from .outbound import dedupe_tags, from_clash_proxies, from_uris, parse_uri
from .subscription import fetch_full, parse


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


def _manual_proxy(kind: str, host: str, port: int, user: str, pw: str) -> dict:
    t = "socks" if kind == "socks" else "http"
    ob: dict = {"type": t, "tag": f"{kind}-{host}:{port}", "server": host, "server_port": int(port)}
    if t == "socks":
        ob["version"] = "5"
    if user:
        ob["username"] = user
    if pw:
        ob["password"] = pw
    return ob


def parse_manual(kind: str, text: str) -> dict:
    """Собрать outbound из ручного ввода.

    kind: 'socks' | 'http' | 'uri'. Форматы:
      host:port | host:port:login:password (для socks/http)
      socks5://user:pass@host:port | http://... | ss:// | vless:// | vmess:// | trojan:// | hy2://
    """
    text = text.strip()
    if kind == "uri" or "://" in text:
        scheme = text.split("://", 1)[0].lower() if "://" in text else ""
        if scheme in ("socks", "socks5", "socks5h", "http", "https"):
            u = up.urlparse(text)
            k = "socks" if scheme.startswith("socks") else "http"
            port = u.port or (1080 if k == "socks" else 8080)
            return _manual_proxy(k, u.hostname or "", port,
                                 up.unquote(u.username or ""), up.unquote(u.password or ""))
        ob = parse_uri(text)
        if ob:
            return ob
        raise ValueError("не понял ссылку (нужно ss:// / vless:// / vmess:// / trojan:// / socks5:// / http://)")
    parts = text.split(":", 3)
    if len(parts) < 2:
        raise ValueError("нужно host:port или host:port:login:password")
    host = parts[0].strip()
    port = int(parts[1].strip())
    user = parts[2].strip() if len(parts) > 2 else ""
    pw = parts[3].strip() if len(parts) > 3 else ""
    k = kind if kind in ("socks", "http") else "socks"
    return _manual_proxy(k, host, port, user, pw)


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
