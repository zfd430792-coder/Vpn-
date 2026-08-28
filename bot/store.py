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
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.keys = data.get("keys", []) or []
        except Exception:
            self.keys = []

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"keys": self.keys}, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except Exception:
                pass

    def add(self, url: str, hwid: str = "", name: str = "") -> Dict:
        for k in self.keys:
            if k.get("url") == url:
                if hwid:
                    k["hwid"] = hwid
                if name:
                    k["name"] = name
                self.save()
                return k
        key = {
            "url": url,
            "hwid": hwid,
            "name": name or default_name(url),
            "added_at": int(time.time()),
            "report": {},
        }
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
