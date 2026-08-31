import base64
import json
import urllib.parse as up
from typing import Any, Dict, List, Optional


def _tag(prefix: str, host: str, port: int) -> str:
    host = (host or "unknown").replace(":", "_")
    return f"{prefix}-{host}-{port}"


def _b64pad(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s)


def from_vmess(uri: str) -> Dict[str, Any]:
    data = json.loads(_b64pad(uri[len("vmess://"):]).decode())
    host = data["add"]
    port = int(data["port"])
    ob: Dict[str, Any] = {
        "type": "vmess",
        "tag": data.get("ps") or _tag("vmess", host, port),
        "server": host,
        "server_port": port,
        "uuid": data["id"],
        "security": data.get("scy", "auto"),
        "alter_id": int(data.get("aid", 0)),
    }
    net = data.get("net", "tcp")
    if net == "ws":
        ob["transport"] = {
            "type": "ws",
            "path": data.get("path", "/"),
            "headers": {"Host": data["host"]} if data.get("host") else {},
        }
    elif net == "grpc":
        ob["transport"] = {"type": "grpc", "service_name": data.get("path", "")}
    if data.get("tls") == "tls":
        ob["tls"] = {
            "enabled": True,
            "server_name": data.get("sni") or data.get("host") or host,
        }
    return ob


# sing-box маскирует REALITY под uTLS-хендшейк: без блока utls рукопожатие
# с нодой не проходит, и прокси отвечает "General SOCKS server failure".
# Подписки часто fingerprint не присылают — подставляем безопасный дефолт.
DEFAULT_UTLS_FINGERPRINT = "chrome"


def _ensure_reality_utls(tls: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not tls or not (tls.get("reality") or {}).get("enabled"):
        return tls
    fp = (tls.get("utls") or {}).get("fingerprint")
    tls["utls"] = {"enabled": True, "fingerprint": fp or DEFAULT_UTLS_FINGERPRINT}
    return tls


def _vless_or_trojan_tls(q: Dict[str, str], host: str) -> Optional[Dict[str, Any]]:
    security = q.get("security", "")
    if security not in ("tls", "reality", "xtls"):
        return None
    tls: Dict[str, Any] = {
        "enabled": True,
        "server_name": q.get("sni") or q.get("host") or host,
        "insecure": q.get("allowInsecure") == "1",
    }
    if q.get("alpn"):
        tls["alpn"] = q["alpn"].split(",")
    if q.get("fp"):
        tls["utls"] = {"enabled": True, "fingerprint": q["fp"]}
    if security == "reality":
        tls["reality"] = {
            "enabled": True,
            "public_key": q.get("pbk", ""),
            "short_id": q.get("sid", ""),
        }
    return _ensure_reality_utls(tls)


def _transport(q: Dict[str, str]) -> Optional[Dict[str, Any]]:
    net = q.get("type", "tcp")
    if net == "ws":
        headers = {"Host": q["host"]} if q.get("host") else {}
        return {"type": "ws", "path": q.get("path", "/"), "headers": headers}
    if net == "grpc":
        return {"type": "grpc", "service_name": q.get("serviceName", "")}
    if net == "httpupgrade":
        return {
            "type": "httpupgrade",
            "path": q.get("path", "/"),
            "host": q.get("host", ""),
        }
    return None


def from_vless(uri: str) -> Dict[str, Any]:
    u = up.urlparse(uri)
    q = dict(up.parse_qsl(u.query))
    ob: Dict[str, Any] = {
        "type": "vless",
        "tag": up.unquote(u.fragment) or _tag("vless", u.hostname or "", u.port or 0),
        "server": u.hostname,
        "server_port": u.port,
        "uuid": u.username,
    }
    if q.get("flow"):
        ob["flow"] = q["flow"]
    tr = _transport(q)
    if tr:
        ob["transport"] = tr
    tls = _vless_or_trojan_tls(q, u.hostname or "")
    if tls:
        ob["tls"] = tls
    return ob


def from_trojan(uri: str) -> Dict[str, Any]:
    u = up.urlparse(uri)
    q = dict(up.parse_qsl(u.query))
    ob: Dict[str, Any] = {
        "type": "trojan",
        "tag": up.unquote(u.fragment) or _tag("trojan", u.hostname or "", u.port or 0),
        "server": u.hostname,
        "server_port": u.port,
        "password": up.unquote(u.username or ""),
        "tls": {
            "enabled": True,
            "server_name": q.get("sni") or q.get("host") or u.hostname,
            "insecure": q.get("allowInsecure") == "1",
        },
    }
    tr = _transport(q)
    if tr:
        ob["transport"] = tr
    return ob


def from_ss(uri: str) -> Dict[str, Any]:
    body = uri[len("ss://"):]
    tag = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        tag = up.unquote(frag)
    at = body.rfind("@")
    if at != -1:
        userinfo = body[:at]
        hostport = body[at + 1:]
        try:
            userinfo = _b64pad(userinfo).decode()
        except Exception:
            pass
    else:
        decoded = _b64pad(body).decode()
        if "@" not in decoded:
            raise ValueError(f"unrecognized ss uri: {uri}")
        userinfo, hostport = decoded.split("@", 1)
    method, password = userinfo.split(":", 1)
    if "?" in hostport:
        hostport = hostport.split("?", 1)[0]
    host, port = hostport.rsplit(":", 1)
    return {
        "type": "shadowsocks",
        "tag": tag or _tag("ss", host, int(port)),
        "server": host,
        "server_port": int(port),
        "method": method,
        "password": password,
    }


def from_hy2(uri: str) -> Dict[str, Any]:
    u = up.urlparse(uri)
    q = dict(up.parse_qsl(u.query))
    return {
        "type": "hysteria2",
        "tag": up.unquote(u.fragment) or _tag("hy2", u.hostname or "", u.port or 0),
        "server": u.hostname,
        "server_port": u.port,
        "password": up.unquote(u.username or "") or q.get("password", ""),
        "tls": {
            "enabled": True,
            "server_name": q.get("sni") or u.hostname,
            "insecure": q.get("insecure") == "1",
        },
    }


PARSERS = {
    "vmess": from_vmess,
    "vless": from_vless,
    "trojan": from_trojan,
    "ss": from_ss,
    "hysteria2": from_hy2,
    "hy2": from_hy2,
}


def parse_uri(uri: str) -> Optional[Dict[str, Any]]:
    scheme = uri.split("://", 1)[0].lower()
    fn = PARSERS.get(scheme)
    if fn is None:
        return None
    try:
        return fn(uri)
    except Exception:
        return None


def from_clash(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    t = (node.get("type") or "").lower()
    server = node.get("server")
    port = node.get("port")
    if not server or port is None:
        return None
    name = node.get("name") or _tag(t or "clash", server, int(port))
    if t in ("ss", "shadowsocks"):
        return {
            "type": "shadowsocks",
            "tag": name,
            "server": server,
            "server_port": int(port),
            "method": node.get("cipher") or node.get("method"),
            "password": node.get("password"),
        }
    if t == "vmess":
        ob: Dict[str, Any] = {
            "type": "vmess",
            "tag": name,
            "server": server,
            "server_port": int(port),
            "uuid": node["uuid"],
            "alter_id": int(node.get("alterId", 0)),
            "security": node.get("cipher", "auto"),
        }
        net = node.get("network", "tcp")
        if net == "ws":
            ws = node.get("ws-opts") or {}
            ob["transport"] = {
                "type": "ws",
                "path": ws.get("path", "/"),
                "headers": ws.get("headers") or {},
            }
        elif net == "grpc":
            g = node.get("grpc-opts") or {}
            ob["transport"] = {
                "type": "grpc",
                "service_name": g.get("grpc-service-name", ""),
            }
        if node.get("tls"):
            ob["tls"] = {
                "enabled": True,
                "server_name": node.get("servername") or node.get("sni") or server,
                "insecure": bool(node.get("skip-cert-verify")),
            }
        return ob
    if t == "trojan":
        ob = {
            "type": "trojan",
            "tag": name,
            "server": server,
            "server_port": int(port),
            "password": node["password"],
            "tls": {
                "enabled": True,
                "server_name": node.get("sni") or server,
                "insecure": bool(node.get("skip-cert-verify")),
            },
        }
        if node.get("network") == "ws":
            ws = node.get("ws-opts") or {}
            ob["transport"] = {
                "type": "ws",
                "path": ws.get("path", "/"),
                "headers": ws.get("headers") or {},
            }
        return ob
    if t == "vless":
        ob = {
            "type": "vless",
            "tag": name,
            "server": server,
            "server_port": int(port),
            "uuid": node["uuid"],
        }
        if node.get("flow"):
            ob["flow"] = node["flow"]
        net = node.get("network", "tcp")
        if net == "ws":
            ws = node.get("ws-opts") or {}
            ob["transport"] = {
                "type": "ws",
                "path": ws.get("path", "/"),
                "headers": ws.get("headers") or {},
            }
        elif net == "grpc":
            g = node.get("grpc-opts") or {}
            ob["transport"] = {
                "type": "grpc",
                "service_name": g.get("grpc-service-name", ""),
            }
        if node.get("tls"):
            tls: Dict[str, Any] = {
                "enabled": True,
                "server_name": node.get("servername") or node.get("sni") or server,
                "insecure": bool(node.get("skip-cert-verify")),
            }
            if node.get("client-fingerprint"):
                tls["utls"] = {
                    "enabled": True,
                    "fingerprint": node["client-fingerprint"],
                }
            r = node.get("reality-opts") or {}
            if r:
                tls["reality"] = {
                    "enabled": True,
                    "public_key": r.get("public-key", ""),
                    "short_id": r.get("short-id", ""),
                }
            ob["tls"] = _ensure_reality_utls(tls)
        return ob
    if t in ("hysteria2", "hy2"):
        return {
            "type": "hysteria2",
            "tag": name,
            "server": server,
            "server_port": int(port),
            "password": node.get("password", ""),
            "tls": {
                "enabled": True,
                "server_name": node.get("sni") or server,
                "insecure": bool(node.get("skip-cert-verify")),
            },
        }
    return None


def dedupe_tags(outbounds: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set = set()
    result = []
    for ob in outbounds:
        base = ob.get("tag") or _tag(ob.get("type", "ob"), ob.get("server", "?"), ob.get("server_port", 0))
        tag = base
        i = 2
        while tag in seen:
            tag = f"{base}#{i}"
            i += 1
        ob["tag"] = tag
        seen.add(tag)
        result.append(ob)
    return result


def from_uris(uris: List[str]) -> List[Dict[str, Any]]:
    parsed = [ob for ob in (parse_uri(u) for u in uris) if ob]
    return dedupe_tags(parsed)


def from_clash_proxies(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parsed = [ob for ob in (from_clash(n) for n in nodes) if ob]
    return dedupe_tags(parsed)


def _xray_stream(ss: Dict[str, Any], host: str) -> Dict[str, Any]:
    """streamSettings из Xray -> transport/tls во внутреннем (sing-box) виде."""
    ss = ss or {}
    out: Dict[str, Any] = {}
    net = str(ss.get("network") or "tcp").lower()
    if net == "ws":
        w = ss.get("wsSettings") or {}
        hdrs = w.get("headers") or {}
        out["transport"] = {
            "type": "ws",
            "path": w.get("path", "/"),
            "headers": {"Host": hdrs["Host"]} if hdrs.get("Host") else {},
        }
    elif net == "grpc":
        g = ss.get("grpcSettings") or {}
        out["transport"] = {"type": "grpc", "service_name": g.get("serviceName", "")}
    elif net == "httpupgrade":
        h = ss.get("httpupgradeSettings") or {}
        out["transport"] = {
            "type": "httpupgrade",
            "path": h.get("path", "/"),
            "host": h.get("host", ""),
        }

    sec = str(ss.get("security") or "none").lower()
    if sec in ("tls", "xtls", "reality"):
        src = (ss.get("realitySettings") if sec == "reality" else ss.get("tlsSettings")) or {}
        tls: Dict[str, Any] = {
            "enabled": True,
            "server_name": src.get("serverName") or src.get("sni") or host,
            "insecure": bool(src.get("allowInsecure")),
        }
        alpn = src.get("alpn")
        if alpn:
            tls["alpn"] = alpn if isinstance(alpn, list) else str(alpn).split(",")
        if src.get("fingerprint"):
            tls["utls"] = {"enabled": True, "fingerprint": src["fingerprint"]}
        if sec == "reality":
            tls["reality"] = {
                "enabled": True,
                "public_key": src.get("publicKey", ""),
                "short_id": src.get("shortId", ""),
            }
        out["tls"] = _ensure_reality_utls(tls)
    return out


def from_xray_outbound(ob: Dict[str, Any], remarks: str = "") -> Optional[Dict[str, Any]]:
    """Один outbound Xray/v2ray-JSON (формат Happ) -> внутренний вид."""
    proto = str(ob.get("protocol") or "").lower()
    st = ob.get("settings") or {}
    name = remarks or str(ob.get("tag") or "")

    if proto in ("vless", "vmess"):
        vnext = st.get("vnext") or []
        if not vnext:
            return None
        v = vnext[0]
        users = v.get("users") or [{}]
        u = users[0] if users else {}
        host = v.get("address")
        port = int(v.get("port") or 0)
        out: Dict[str, Any] = {
            "type": proto,
            "tag": name or _tag(proto, str(host or ""), port),
            "server": host,
            "server_port": port,
            "uuid": u.get("id", ""),
        }
        if proto == "vless":
            if u.get("flow"):
                out["flow"] = u["flow"]
        else:
            out["alter_id"] = int(u.get("alterId") or 0)
            out["security"] = u.get("security") or "auto"
        out.update(_xray_stream(ob.get("streamSettings") or {}, str(host or "")))
        return out

    if proto in ("trojan", "shadowsocks"):
        servers = st.get("servers") or []
        if not servers:
            return None
        s = servers[0]
        host = s.get("address")
        port = int(s.get("port") or 0)
        if proto == "trojan":
            out = {
                "type": "trojan",
                "tag": name or _tag("trojan", str(host or ""), port),
                "server": host,
                "server_port": port,
                "password": s.get("password", ""),
            }
            out.update(_xray_stream(ob.get("streamSettings") or {}, str(host or "")))
            return out
        return {
            "type": "shadowsocks",
            "tag": name or _tag("ss", str(host or ""), port),
            "server": host,
            "server_port": port,
            "method": s.get("method", ""),
            "password": s.get("password", ""),
        }
    return None


def from_xray_configs(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Массив Xray-конфигов (Happ отдаёт подписку именно так) -> outbound'ы."""
    result: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        remarks = str(item.get("remarks") or "")
        for ob in item.get("outbounds") or []:
            if not isinstance(ob, dict):
                continue
            conv = from_xray_outbound(ob, remarks)
            if conv:
                result.append(conv)
    return dedupe_tags(result)
