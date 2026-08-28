import json
import os
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse


def default_name(url: str) -> str:
    host = urlparse(url).hostname or "key"
    tail = url[-4:] if len(url) >= 4 else url
    return f"{host}…{tail}"


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        self.keys: List[Dict] = []
        self.targets: List[str] = []
        self.servers: List[Dict] = []
        self.settings: Dict = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.keys = data.get("keys", []) or []
            self.targets = data.get("targets", []) or []
            self.servers = data.get("servers", []) or []
            self.settings = data.get("settings", {}) or {}
        except Exception:
            self.keys = []
            self.targets = []
            self.servers = []
            self.settings = {}

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(
                        {"keys": self.keys, "targets": self.targets,
                         "servers": self.servers, "settings": self.settings},
                        f, ensure_ascii=False, indent=2,
                    )
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except Exception:
                pass

    # ---- keys ----
    def add(self, url: str, hwid: str = "", name: str = "") -> Dict:
        for k in self.keys:
            if k.get("url") == url:
                if hwid:
                    k["hwid"] = hwid
                if name:
                    k["name"] = name
                self.save()
                return k
        key = {"url": url, "hwid": hwid, "name": name or default_name(url),
               "added_at": int(time.time()), "report": {}}
        self.keys.append(key)
        self.save()
        return key

    def remove(self, idx: int) -> Optional[Dict]:
        if 0 <= idx < len(self.keys):
            k = self.keys.pop(idx)
            self.save()
            return k
        return None

    def get(self, idx: int) -> Optional[Dict]:
        if 0 <= idx < len(self.keys):
            return self.keys[idx]
        return None

    def index_of(self, url: str) -> int:
        for i, k in enumerate(self.keys):
            if k.get("url") == url:
                return i
        return -1

    # ---- download targets ----
    def add_target(self, url: str) -> bool:
        if url in self.targets:
            return False
        self.targets.append(url)
        self.save()
        return True

    def remove_target(self, idx: int) -> Optional[str]:
        if 0 <= idx < len(self.targets):
            t = self.targets.pop(idx)
            self.save()
            return t
        return None

    # ---- agent servers (extra burner machines) ----
    def add_server(self, url: str, token: str = "", name: str = "") -> Dict:
        url = url.rstrip("/")
        for s in self.servers:
            if s.get("url") == url:
                s["token"] = token
                if name:
                    s["name"] = name
                self.save()
                return s
        srv = {"url": url, "token": token, "name": name or (urlparse(url).hostname or url)}
        self.servers.append(srv)
        self.save()
        return srv

    def remove_server(self, idx: int) -> Optional[Dict]:
        if 0 <= idx < len(self.servers):
            s = self.servers.pop(idx)
            self.save()
            return s
        return None

    # ---- settings ----
    def set_setting(self, key: str, value) -> None:
        self.settings[key] = value
        self.save()
