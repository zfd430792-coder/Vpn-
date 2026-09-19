"""Конфигурация. Читается из окружения — в проде это /etc/anime-bot/env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# код плана -> (подпись, дней, цена в звёздах по умолчанию)
DEFAULT_PLANS: dict[str, tuple[str, int, int]] = {
    "week": ("Неделя", 7, 50),
    "month": ("Месяц", 30, 150),
    "quarter": ("3 месяца", 90, 350),
    "life": ("Навсегда", 36500, 1000),
}


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _int(key: str, default: int = 0) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _ids(key: str) -> set[int]:
    out: set[int] = set()
    for chunk in _env(key).replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return out


@dataclass
class Config:
    bot_token: str = ""
    admins: set[int] = field(default_factory=set)
    api_id: int = 0
    api_hash: str = ""
    session: str = ""
    storage_channel: int = 0
    channel_title: str = "Anime Storage"
    data_dir: Path = Path("/var/lib/anime-bot")
    free_episodes: int = 3
    protect_content: bool = True
    autodelete: int = 0
    delivery: str = "bot"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "anime.db"

    @property
    def env_path(self) -> Path:
        """Файл, куда setup дописывает SESSION и STORAGE_CHANNEL."""
        return Path(_env("ENV_FILE") or "/etc/anime-bot/env")

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def check(self) -> list[str]:
        """Список проблем, из-за которых бот не взлетит."""
        problems = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан")
        if not self.admins:
            problems.append("ADMINS не задан — админку будет некому открыть")
        if not self.storage_channel:
            problems.append("STORAGE_CHANNEL не задан — запусти: python -m anibot.setup")
        return problems


def load() -> Config:
    cfg = Config(
        bot_token=_env("BOT_TOKEN"),
        admins=_ids("ADMINS") or _ids("ADMIN_ID"),
        api_id=_int("API_ID"),
        api_hash=_env("API_HASH"),
        session=_env("SESSION"),
        storage_channel=_int("STORAGE_CHANNEL"),
        channel_title=_env("CHANNEL_TITLE") or "Anime Storage",
        data_dir=Path(_env("DATA_DIR") or "/var/lib/anime-bot"),
        free_episodes=_int("FREE_EPISODES", 3),
        protect_content=_bool("PROTECT_CONTENT", True),
        autodelete=_int("AUTODELETE", 0),
        delivery=(_env("DELIVERY") or "bot").lower(),
    )
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
