"""Кто хостит ноды подписки.

Адреса нод резолвим в IP, затем спрашиваем ip-api.com про владельца сети:
ASN, провайдера и страну. Сервис бесплатный, без ключа, до 100 адресов за
запрос и не больше 45 запросов в минуту — для подписки на сотню нод это один
батч. Работает только по http, https там платный.
"""
import json
import socket
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Set, Tuple

API = ("http://ip-api.com/batch"
       "?fields=status,query,country,countryCode,isp,org,as,asname,hosting")
BATCH = 100


def _resolve(host: str) -> str:
    """Имя -> IP. Уже готовый IP возвращаем как есть, при неудаче — пусто."""
    if not host:
        return ""
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass
    try:
        return socket.gethostbyname(host)
    except Exception:
        return ""


def resolve_hosts(hosts: Iterable[str], workers: int = 16) -> Dict[str, str]:
    uniq = sorted({str(h or "").strip() for h in hosts if str(h or "").strip()})
    if not uniq:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(uniq))) as ex:
        ips = list(ex.map(_resolve, uniq))
    return dict(zip(uniq, ips))


def lookup_ips(ips: Iterable[str], timeout: int = 25) -> Dict[str, dict]:
    uniq = [i for i in sorted({str(i or "") for i in ips}) if i]
    out: Dict[str, dict] = {}
    for start in range(0, len(uniq), BATCH):
        chunk = uniq[start:start + BATCH]
        req = urllib.request.Request(
            API, data=json.dumps(chunk).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                rows = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("status") == "success" and row.get("query"):
                out[row["query"]] = row
    return out


def _org_of(row: dict) -> str:
    """Понятное имя владельца сети: сначала ASN-имя, потом провайдер."""
    for key in ("asname", "isp", "org"):
        v = str(row.get(key) or "").strip()
        if v:
            return v
    return "неизвестно"


def _asn_of(row: dict) -> str:
    a = str(row.get("as") or "").strip()
    return a.split()[0] if a else ""


def summarize(outbounds: List[dict]) -> Tuple[List[dict], int, int]:
    """(группы по хостеру, нод без ответа, всего уникальных адресов).

    Группа: {org, asn, nodes, ips, countries, hosting}.
    """
    hosts = [str(o.get("server") or "") for o in outbounds]
    hmap = resolve_hosts(hosts)
    info = lookup_ips([ip for ip in hmap.values() if ip])

    groups: Dict[str, dict] = {}
    unknown = 0
    for ob in outbounds:
        ip = hmap.get(str(ob.get("server") or ""), "")
        row = info.get(ip)
        if not row:
            unknown += 1
            continue
        org = _org_of(row)
        g = groups.setdefault(org, {
            "org": org, "asn": _asn_of(row), "nodes": 0,
            "ips": set(), "countries": set(), "hosting": False,
        })
        g["nodes"] += 1
        g["ips"].add(ip)
        cc = str(row.get("countryCode") or "").strip()
        if cc:
            g["countries"].add(cc)
        if row.get("hosting"):
            g["hosting"] = True

    ordered = sorted(groups.values(), key=lambda g: (-g["nodes"], g["org"]))
    resolved = len({ip for ip in hmap.values() if ip})
    return ordered, unknown, resolved
