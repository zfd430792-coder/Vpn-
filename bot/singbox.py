import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple


@lru_cache(maxsize=8)
def singbox_version(binary: str) -> Tuple[int, int]:
    """(major, minor) установленного sing-box; (0, 0) — определить не вышло."""
    exe = shutil.which(binary) or binary
    try:
        out = subprocess.run([exe, "version"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return (0, 0)
    m = re.search(r"version\s+v?(\d+)\.(\d+)", out or "")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


LOG_PATH = "/tmp/vpn-singbox.log"


def build_config(outbounds: List[Dict[str, Any]], socks_port: int,
                 log_level: str = "warn",
                 binary: Optional[str] = None) -> Dict[str, Any]:
    if not outbounds:
        raise ValueError("no outbounds")
    tags = [o["tag"] for o in outbounds]
    # Резолвим адрес ноды сначала в IPv4: на IPv4-only хостах AAAA даёт
    # "Network unreachable". prefer_ipv4 = пробуем A, при отсутствии — AAAA.
    # С sing-box 1.12 domain_strategy внутри outbound'а объявлен legacy и
    # роняет запуск ("legacy domain strategy options is deprecated"), поэтому
    # там же настройка задаётся через route.default_domain_resolver.
    ver = singbox_version(binary or os.environ.get("SINGBOX_BIN") or "sing-box")
    modern = ver >= (1, 12) or ver == (0, 0)  # версию не узнали — считаем свежей
    real = []
    for o in outbounds:
        o = dict(o)
        if not modern:
            o.setdefault("domain_strategy", "prefer_ipv4")
        real.append(o)
    # Отдельный вход на каждую ноду: порт (socks_port + i) жёстко ведёт в ноду i,
    # чтобы воркеры качали через все ноды сразу и их полосы складывались.
    inbounds = []
    rules = []
    for i, tag in enumerate(tags):
        itag = f"in-{i}"
        inbounds.append({
            "type": "mixed",
            "tag": itag,
            "listen": "127.0.0.1",
            "listen_port": socks_port + i,
        })
        rules.append({"inbound": [itag], "outbound": tag})
    config: Dict[str, Any] = {
        "log": {"level": log_level},
        "inbounds": inbounds,
        "outbounds": [
            {
                "type": "urltest",
                "tag": "auto",
                "outbounds": tags,
                "url": "https://www.gstatic.com/generate_204",
                "interval": "1m",
                "tolerance": 100,
            },
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": ["auto", *tags],
                "default": "auto",
            },
            *real,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"rules": rules, "final": "auto"},
        "experimental": {
            "clash_api": {"external_controller": "127.0.0.1:9090"}
        },
    }
    if modern:
        config["dns"] = {"servers": [{"type": "local", "tag": "local"}]}
        config["route"]["default_domain_resolver"] = {
            "server": "local",
            "strategy": "prefer_ipv4",
        }
    return config


def wait_port(host: str, port: int, timeout: float = 15) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"sing-box did not bind {host}:{port} within {timeout}s")


class SingBox:
    def __init__(self, binary: str = "sing-box"):
        resolved = shutil.which(binary) or binary
        self.binary = resolved
        self.proc: Optional[subprocess.Popen] = None
        self.cfg_path: Optional[str] = None
        self.log_path: Optional[str] = None
        self.log_file = None

    def start(self, config: Dict[str, Any], socks_port: int) -> None:
        if not shutil.which(self.binary) and not os.path.isfile(self.binary):
            raise RuntimeError(
                f"sing-box binary not found ({self.binary!r}). "
                "Install from https://sing-box.sagernet.org/installation/ "
                "or pass --singbox /path/to/sing-box."
            )
        fd, path = tempfile.mkstemp(suffix=".json", prefix="singbox-")
        os.close(fd)
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
        self.cfg_path = path
        # Раньше вывод уходил в /dev/null, и настоящая причина отказов нод
        # ("REALITY handshake failed", "dns: no such host", "network is
        # unreachable") была не видна — наверх всплывал только обёрточный
        # "General SOCKS server failure" от SOCKS-клиента. Пишем в файл.
        self.log_path = LOG_PATH
        try:
            self.log_file = open(self.log_path, "w", encoding="utf-8", errors="replace")
        except OSError:
            self.log_file = None
        self.proc = subprocess.Popen(
            [self.binary, "run", "-c", path],
            stdout=self.log_file or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_port("127.0.0.1", socks_port, timeout=15)
        except Exception:
            self.stop()
            raise

    def tail_log(self, lines: int = 12) -> str:
        """Последние строки лога sing-box — настоящая причина отказа нод."""
        if not self.log_path:
            return ""
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
                rows = [r.rstrip() for r in f.readlines() if r.strip()]
        except OSError:
            return ""
        return "\n".join(rows[-lines:])

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.log_file:
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None
        if self.cfg_path and os.path.exists(self.cfg_path):
            try:
                os.unlink(self.cfg_path)
            except OSError:
                pass
        self.cfg_path = None
