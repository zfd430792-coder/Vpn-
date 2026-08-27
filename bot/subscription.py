import base64
from typing import Any, Dict

import requests
import yaml


DEFAULT_UA = "v2rayN/6.42"


def fetch(url: str, ua: str = DEFAULT_UA, timeout: int = 20) -> str:
    resp = requests.get(url, headers={"User-Agent": ua}, timeout=timeout)
    resp.raise_for_status()
    return resp.text.strip()


def _try_b64(payload: str) -> str | None:
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


def parse(body: str) -> Dict[str, Any]:
    body = body.strip()
    decoded = _try_b64(body)
    if decoded is not None:
        body = decoded.strip()
    head = body[:2000]
    if body.startswith("{") and '"outbounds"' in head:
        import json
        return {"kind": "singbox", "config": json.loads(body)}
    if "proxies:" in head:
        conf = yaml.safe_load(body) or {}
        return {"kind": "clash", "proxies": conf.get("proxies", []) or []}
    uris = [line.strip() for line in body.splitlines() if "://" in line]
    return {"kind": "uris", "uris": uris}
