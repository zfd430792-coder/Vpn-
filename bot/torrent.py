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

# Встроенные раздачи. Берём .torrent, а не magnet: в magnet нет списка
# файлов, его выкачивают у пиров, и без пиров торрент намертво встаёт на
# "жду метаданные". В .torrent метаданные уже внутри — качать можно сразу,
# как только найдётся хоть один пир.
# Официальные образы Ubuntu: тысячи сидов, законно, abuse-жалоб не будет.
DEFAULT_SOURCES = [
    "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-live-server-amd64.iso.torrent",
    "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-desktop-amd64.iso.torrent",
]
DEFAULT_MAGNETS = DEFAULT_SOURCES  # старое имя, чтобы не ломать импорты

# Публичные open-трекеры по UDP. У образов Ubuntu трекеры только HTTPS, а их
# announce зашифрован — anti-torrent детекторы его обычно не видят. UDP-announce
# идёт открыто и распознаётся сразу, плюс такие трекеры дают заметно больше
# пиров. Недоступные из списка просто отвалятся с ошибкой и никому не мешают.
EXTRA_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]


def available() -> bool:
    return lt is not None


class TorrentBurner:
    def __init__(self, socks_host: str, socks_port: int, save_dir: str,
                 cap_per_torrent: int = DEFAULT_CAP, extra_trackers: bool = True):
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.save_dir = save_dir
        self.cap = cap_per_torrent
        self.extra_trackers = extra_trackers
        self.ses = None
        self.handles: List = []
        self.sources: List[str] = []
        self.total_bytes = 0     # накопительно за сессию, переживает перезапуски торрентов
        self._counted: Dict[str, int] = {}
        self.errors = 0
        self.last_error = ""
        self.started_at = 0.0
        self.tracker_ok = False
        self.tracker_peers = 0

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
            # DHT даёт пиров, когда трекер молчит. Он по UDP, и через SOCKS5
            # работает не везде, но force_proxy не даст ему уйти мимо ноды:
            # в худшем случае просто не заработает.
            "enable_dht": True,
            "enable_lsd": False,
            "enable_natpmp": False,
            "enable_upnp": False,
            "enable_incoming_utp": False,
            "enable_outgoing_utp": False,
            # Без алертов торрент молчит, и причину простоя (трекер не
            # отвечает, метаданные не приходят) взять негде.
            "alert_mask": (lt.alert.category_t.error_notification
                           | lt.alert.category_t.tracker_notification
                           | lt.alert.category_t.status_notification),
            # Через SOCKS входящих соединений не будет: слушать порт незачем,
            # а анонс с локальных интерфейсов только плодит ошибки.
            "listen_interfaces": "127.0.0.1:0",
            "announce_to_all_trackers": True,
            "announce_to_all_tiers": True,
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

    @staticmethod
    def _drop_ipv6_trackers(h) -> None:
        """Убрать IPv6-трекеры у добавленного торрента: на IPv4-сервере они
        недостижимы и только копят ошибки, забивая настоящую причину."""
        try:
            # у torrent_handle трекеры приходят словарями, у torrent_info —
            # объектами; поддерживаем оба вида
            urls = [t["url"] if isinstance(t, dict) else t.url for t in h.trackers()]
        except Exception:  # noqa: BLE001
            return
        keep = [u for u in urls if "ipv6." not in u]
        if keep and len(keep) != len(urls):
            try:
                h.replace_trackers([{"url": u, "tier": i} for i, u in enumerate(keep)])
            except Exception:  # noqa: BLE001
                pass

    def _enrich_trackers(self, h) -> None:
        """Досыпать публичные UDP-трекеры: больше пиров и открытый announce."""
        if not self.extra_trackers:
            return
        try:
            have = {t["url"] if isinstance(t, dict) else t.url for t in h.trackers()}
        except Exception:  # noqa: BLE001
            have = set()
        for url in EXTRA_TRACKERS:
            if url not in have:
                try:
                    h.add_tracker({"url": url, "tier": 1})
                except Exception:  # noqa: BLE001
                    pass

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
        self._drop_ipv6_trackers(h)
        self._enrich_trackers(h)
        self.handles.append(h)
        return h

    # ---------- работа ----------
    def _key(self, h) -> str:
        try:
            return str(h.info_hash())
        except Exception:  # noqa: BLE001
            return str(id(h))

    STATE_RU = {
        "checking_files": "проверка файлов",
        "downloading_metadata": "жду метаданные (нужны пиры)",
        "downloading": "качаю",
        "finished": "готово",
        "seeding": "раздаю",
        "allocating": "выделяю место",
        "checking_resume_data": "проверка",
    }

    def _drain_alerts(self) -> None:
        """Забрать сообщения libtorrent — там причина, если торрент стоит."""
        try:
            alerts = self.ses.pop_alerts()
        except Exception:  # noqa: BLE001
            return
        for a in alerts:
            name = type(a).__name__
            if name in ("tracker_error_alert", "scrape_failed_alert"):
                msg = a.message()
                # libtorrent анонсируется с каждого локального интерфейса, и
                # для loopback/0.0.0.0 это заведомо недостижимо. Такие строки —
                # шум, который забивает настоящую причину.
                if "unreachable" in msg or "skipping tracker announce" in msg:
                    continue
                self.errors += 1
                self.last_error = f"трекер: {msg}"
            elif name in ("session_error_alert", "torrent_error_alert",
                          "peer_error_alert", "udp_error_alert"):
                self.errors += 1
                self.last_error = a.message()
            elif name == "tracker_reply_alert":
                got = int(getattr(a, "num_peers", 0) or 0)
                self.tracker_peers = max(self.tracker_peers, got)
                self.tracker_ok = True

    def poll(self) -> None:
        """Учесть скачанное и перезапустить торренты, упёршиеся в лимит."""
        if not self.ses:
            return
        self._drain_alerts()
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
        state = ""
        for h in list(self.handles):
            try:
                st = h.status()
            except Exception:  # noqa: BLE001
                continue
            rate += int(getattr(st, "download_payload_rate", 0) or 0)
            peers += int(getattr(st, "num_peers", 0) or 0)
            if not state:
                raw = str(getattr(st, "state", "") or "")
                state = self.STATE_RU.get(raw, raw)
            if getattr(st, "num_peers", 0):
                active += 1
        return {"bytes": self.total_bytes, "rate": rate, "peers": peers,
                "torrents": len(self.handles), "active": active,
                "errors": self.errors, "last_error": self.last_error,
                "state": state, "tracker_ok": self.tracker_ok,
                "tracker_peers": self.tracker_peers}

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
