import base64
import json
from typing import Any, Dict, Optional, Tuple

import requests
import yaml


DEFAULT_UA = "v2rayN/6.42"


def fetch_full(
    url: str,
    ua: str = DEFAULT_UA,
    timeout: int = 20,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str]]:
    h = {"User-Agent": ua, "Accept": "*/*"}
    if headers:
        h.update(headers)
    resp = requests.get(url, headers=h, timeout=timeout)
    resp.raise_for_status()
    return resp.text.strip(), dict(resp.headers)


def fetch(
    url: str,
    ua: str = DEFAULT_UA,
    timeout: int = 20,
    headers: Optional[Dict[str, str]] = None,
) -> str:
    text, _ = fetch_full(url, ua=ua, timeout=timeout, headers=headers)
    return text


def _try_b64(payload: str) -> Optional[str]:
    stripped = "".join(payload.split())
    stripped += "=" * (-len(stripped) % 4)
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            decoded = decoder(stripped).decode("utf-8", errors="ignore")
        except Exception:
            continue
        if "://" in decoded or "proxies:" in decoded or '"outbounds"' in decoded:
            return decoded
    return None


def _is_xray_outbounds(obs: Any) -> bool:
    """Xray-outbound опознаётся по ключу protocol; sing-box — по type."""
    return any(isinstance(o, dict) and "protocol" in o for o in (obs or []))


def parse(body: str) -> Dict[str, Any]:
    body = body.strip()
    decoded = _try_b64(body)
    if decoded is not None:
        body = decoded.strip()
    head = body[:2000]

    # Happ отдаёт подписку массивом Xray-конфигов: [{"remarks":…,"outbounds":[…]}]
    if body.startswith("["):
        try:
            data = json.loads(body)
        except Exception:
            data = None
        if isinstance(data, list):
            configs = [x for x in data if isinstance(x, dict) and "outbounds" in x]
            if configs:
                return {"kind": "xray", "configs": configs}

    if body.startswith("{") and '"outbounds"' in head:
        conf = json.loads(body)
        obs = conf.get("outbounds") or []
        if _is_xray_outbounds(obs):
            return {"kind": "xray", "configs": [conf]}
        return {"kind": "singbox", "config": conf}

    if "proxies:" in head:
        conf = yaml.safe_load(body) or {}
        return {"kind": "clash", "proxies": conf.get("proxies", []) or []}

    uris = [line.strip() for line in body.splitlines() if "://" in line]
    return {"kind": "uris", "uris": uris}
