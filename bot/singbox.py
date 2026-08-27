import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional


def build_config(outbounds: List[Dict[str, Any]], socks_port: int, log_level: str = "warn") -> Dict[str, Any]:
    if not outbounds:
        raise ValueError("no outbounds")
    tags = [o["tag"] for o in outbounds]
    return {
        "log": {"level": log_level},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": socks_port,
            }
        ],
        "outbounds": [
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": tags,
                "default": tags[0],
            },
            {
                "type": "urltest",
                "tag": "auto",
                "outbounds": tags,
                "url": "https://www.gstatic.com/generate_204",
                "interval": "5m",
            },
            *outbounds,
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"final": "proxy"},
        "experimental": {
            "clash_api": {"external_controller": "127.0.0.1:9090"}
        },
    }


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
        self.proc = subprocess.Popen(
            [self.binary, "run", "-c", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        try:
            wait_port("127.0.0.1", socks_port, timeout=15)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        if self.cfg_path and os.path.exists(self.cfg_path):
            try:
                os.unlink(self.cfg_path)
            except OSError:
                pass
        self.cfg_path = None
