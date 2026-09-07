# -*- coding: utf-8 -*-
"""Жор трафика через BitTorrent.

CDN режут одинокого клиента, который часами тянет один файл, — торренты
этим не страдают: десятки пиров, десятки параллельных потоков, единого
рейт-лимитера нет.

Весь трафик идёт через ноду подписки: libtorrent проксирует и пиров, и
трекеры в SOCKS5, а force_proxy запрещает соединения мимо. DHT и uTP
работают по UDP и через SOCKS5 текут мимо туннеля, поэтому выключены.

Диск не растёт: как только торрент скачал свою порцию, данные удаляются
и закачка начинается заново — байты считаются, место не занимается.
"""
import asyncio
import os
import shutil
import time
from typing import Dict, List, Optional

try:
    import libtorrent as lt
except ImportError:  # noqa: WPS440
    lt = None


DEFAULT_CAP = 2 << 30  # сколько один торрент качает, прежде чем начать заново

# Встроенные раздачи на случай, когда своих не добавили. Официальные образы
# Ubuntu: тысячи сидов, канал забивают целиком, и это законно — abuse-жалоб
# хостеру не будет. infohash взяты из .torrent с releases.ubuntu.com.
_UBUNTU_TRACKERS = ("&tr=https%3A%2F%2Ftorrent.ubuntu.com%2Fannounce"
                    "&tr=https%3A%2F%2Fipv6.torrent.ubuntu.com%2Fannounce")
DEFAULT_MAGNETS = [
    "magnet:?xt=urn:btih:01c137287d6f0ed05a56742dae794f632c79ff3d"
    "&dn=ubuntu-24.04.4-desktop-amd64.iso" + _UBUNTU_TRACKERS,
    "magnet:?xt=urn:btih:62a4d9e139f3315f8716bcccca0cc984a9809da1"
    "&dn=ubuntu-24.04.4-live-server-amd64.iso" + _UBUNTU_TRACKERS,
]


def available() -> bool:
    return lt is not None


class TorrentBurner:
    def __init__(self, socks_host: str, socks_port: int, save_dir: str,
                 cap_per_torrent: int = DEFAULT_CAP):
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.save_dir = save_dir
        self.cap = cap_per_torrent
        self.ses = None
        self.handles: List = []
        self.sources: List[str] = []
        self.total_bytes = 0     # накопительно за сессию, переживает перезапуски торрентов
        self._counted: Dict[str, int] = {}
        self.errors = 0
        self.last_error = ""
        self.started_at = 0.0

    # ---------- запуск ----------
    def _settings(self) -> dict:
        return {
            "proxy_type": 2,  # socks5
            "proxy_hostname": self.socks_host,
            "proxy_port": int(self.socks_port),
            "proxy_peer_connections": True,
            "proxy_tracker_connections": True,
            "proxy_hostnames": True,
            "force_proxy": True,      # ни одного соединения мимо туннеля
            "anonymous_mode": True,
            "enable_dht": False,      # UDP мимо SOCKS5 — выключаем
            "enable_lsd": False,
            "enable_natpmp": False,
            "enable_upnp": False,
            "enable_incoming_utp": False,
            "enable_outgoing_utp": False,
            "alert_mask": 0,
            "connections_limit": 800,
            "active_downloads": -1,
            "active_limit": -1,
        }

    def start(self, sources: List[str]) -> int:
        if lt is None:
            raise RuntimeError(
                "нет модуля libtorrent — поставь: "
                "/opt/vpn-traffic-bot/.venv/bin/pip install libtorrent")
        if not sources:
            raise RuntimeError("не задано ни одной раздачи")
        os.makedirs(self.save_dir, exist_ok=True)
        self.ses = lt.session(self._settings())
        self.sources = list(sources)
        self.started_at = time.monotonic()
        for src in self.sources:
            try:
                self._add(src)
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"
        if not self.handles:
            raise RuntimeError(f"ни одна раздача не добавилась ({self.last_error})")
        return len(self.handles)

    def _add(self, src: str):
        src = src.strip()
        if src.startswith("magnet:"):
            params = lt.parse_magnet_uri(src)
        elif src.lower().startswith(("http://", "https://")):
            # .torrent-файл весит копейки, тянем напрямую — на счётчик не влияет
            import requests
            r = requests.get(src, timeout=30)
            r.raise_for_status()
            params = lt.add_torrent_params()
            params.ti = lt.torrent_info(lt.bdecode(r.content))
        else:
            raise ValueError("нужна magnet-ссылка или ссылка на .torrent")
        params.save_path = self.save_dir
        h = self.ses.add_torrent(params)
        self.handles.append(h)
        return h

    # ---------- работа ----------
    def _key(self, h) -> str:
        try:
            return str(h.info_hash())
        except Exception:  # noqa: BLE001
            return str(id(h))

    def poll(self) -> None:
        """Учесть скачанное и перезапустить торренты, упёршиеся в лимит."""
        if not self.ses:
            return
        for h in list(self.handles):
            try:
                st = h.status()
            except Exception:  # noqa: BLE001
                continue
            key = self._key(h)
            got = int(getattr(st, "total_payload_download", 0) or 0)
            prev = self._counted.get(key, 0)
            if got > prev:
                self.total_bytes += got - prev
                self._counted[key] = got
            # порция скачана — стираем данные и качаем заново, диск не растёт
            if got >= self.cap or getattr(st, "is_seeding", False):
                self._restart(h, key)

    def _restart(self, h, key: str) -> None:
        try:
            url = str(h.status().name or "")
        except Exception:  # noqa: BLE001
            url = ""
        try:
            self.ses.remove_torrent(h, lt.session.delete_files)
        except Exception:  # noqa: BLE001
            pass
        self.handles = [x for x in self.handles if x is not h]
        self._counted.pop(key, None)
        # добавляем ту же раздачу заново — счётчик total_bytes уже сохранён
        src = None
        for s in self.sources:
            if url and url.split()[0][:12].lower() in s.lower():
                src = s
                break
        src = src or (self.sources[0] if self.sources else None)
        if src:
            try:
                self._add(src)
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                self.last_error = f"{type(e).__name__}: {e}"

    def stats(self) -> dict:
        rate = 0
        peers = 0
        active = 0
        for h in list(self.handles):
            try:
                st = h.status()
            except Exception:  # noqa: BLE001
                continue
            rate += int(getattr(st, "download_payload_rate", 0) or 0)
            peers += int(getattr(st, "num_peers", 0) or 0)
            if getattr(st, "num_peers", 0):
                active += 1
        return {"bytes": self.total_bytes, "rate": rate, "peers": peers,
                "torrents": len(self.handles), "active": active,
                "errors": self.errors, "last_error": self.last_error}

    def stop(self) -> None:
        if self.ses:
            for h in list(self.handles):
                try:
                    self.ses.remove_torrent(h, lt.session.delete_files)
                except Exception:  # noqa: BLE001
                    pass
        self.handles = []
        self.ses = None
        try:
            shutil.rmtree(self.save_dir, ignore_errors=True)
        except OSError:
            pass


async def burn_torrents(burner: TorrentBurner, sources: List[str],
                        limit_bytes: int, stop: asyncio.Event,
                        stall_seconds: int = 180) -> None:
    """Крутить торренты, пока не выберем лимит или не встанет трафик."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: burner.start(sources))
    last, last_t = 0, time.monotonic()
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=3)
                return
            except asyncio.TimeoutError:
                pass
            await loop.run_in_executor(None, burner.poll)
            cur = burner.total_bytes
            if limit_bytes and cur >= limit_bytes:
                stop.set()
                return
            if cur > last:
                last, last_t = cur, time.monotonic()
            elif time.monotonic() - last_t >= stall_seconds:
                stop.set()
                return
    finally:
        await loop.run_in_executor(None, burner.stop)
