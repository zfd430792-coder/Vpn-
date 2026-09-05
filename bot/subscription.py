import base64
import gzip
import json
import zlib
from typing import Any, Dict, Optional, Tuple

import requests
import yaml


DEFAULT_UA = "v2rayN/6.42"


def _decompress(raw: bytes, enc: str) -> bytes:
    """Разжать тело подписки.

    Кривые панели ставят Content-Encoding: gzip, а шлют не-gzip (или уже
    plain/base64). requests верит заголовку и падает с
    "incorrect header check". Поэтому берём СЫРЫЕ байты и пробуем разжать
    сами: сначала по заявленной кодировке, затем по магическим байтам, а
    если ничего не подошло — возвращаем как есть (это уже plain-текст).
    """
    if not raw:
        return raw
    enc = (enc or "").lower()

    def _gz(b: bytes) -> bytes:
        return gzip.decompress(b)

    def _zlib_std(b: bytes) -> bytes:
        return zlib.decompress(b)

    def _deflate_raw(b: bytes) -> bytes:
        return zlib.decompress(b, -zlib.MAX_WBITS)

    def _brotli(b: bytes) -> bytes:
        import brotli  # опционально: модуля может не быть
        return brotli.decompress(b)

    order = []
    if "gzip" in enc or "x-gzip" in enc:
        order += [_gz]
    if "deflate" in enc:
        order += [_zlib_std, _deflate_raw]
    if "br" in enc:
        order += [_brotli]
    # заголовок мог соврать — доверяем магическим байтам тела
    if raw[:2] == b"\x1f\x8b":
        order += [_gz]
    elif raw[:1] == b"\x78":  # zlib (0x78 0x01/0x9c/0xda)
        order += [_zlib_std, _deflate_raw]

    for fn in order:
        try:
            out = fn(raw)
            if out:
                return out
        except Exception:
            continue
    return raw  # не сжато либо заголовок фальшивый — отдаём как пришло


def fetch_full(
    url: str,
    ua: str = DEFAULT_UA,
    timeout: int = 20,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, str]]:
    # Accept-Encoding: identity — просим сервер не жать вовсе. Если он всё
    # равно сожмёт (или соврёт про кодировку), разожмём сами из сырых байт,
    # поэтому resp.text (с авто-декодом requests) не трогаем.
    h = {"User-Agent": ua, "Accept": "*/*", "Accept-Encoding": "identity"}
    if headers:
        h.update(headers)
    resp = requests.get(url, headers=h, timeout=timeout, stream=True)
    resp.raise_for_status()
    raw = resp.raw.read(decode_content=False)
    body = _decompress(raw, resp.headers.get("Content-Encoding", ""))
    return body.decode("utf-8", errors="replace").strip(), dict(resp.headers)


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
