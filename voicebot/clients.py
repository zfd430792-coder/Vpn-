"""Какой клиент Telegram записал голосовое — по строке Vendor."""

from __future__ import annotations

from dataclasses import dataclass

# Точные строки Vendor. Нашёл новую — допиши сюда.
KNOWN: dict[str, str] = {
    "libopus 1.5.1": "iPhone",
    "libopus unknown-fixed": "Android",
    "Lavf60.16.101": "Telegram Desktop",
    "libopus 1.3.1-fixed": "macOS (нативный клиент)",
    "libopus unknown": "Telegram X",
    "tweb": "Telegram Web K",
    "telegram-web-a": "Telegram Web A",
}

# Запасной вариант, когда точной строки нет: клиенты обновляются, номер
# версии в строке меняется, а узнаваемая часть остаётся.
FAMILIES: tuple[tuple[str, str], ...] = (
    ("Lavf", "Telegram Desktop"),  # FFmpeg (libavformat) — так пишет Desktop
    ("telegram-web-a", "Telegram Web A"),
    ("tweb", "Telegram Web K"),
)


@dataclass(frozen=True)
class Guess:
    client: str
    exact: bool  # False — совпала только узнаваемая часть, версия другая


def identify(vendor: str) -> Guess | None:
    vendor = vendor.strip()
    if vendor in KNOWN:
        return Guess(KNOWN[vendor], exact=True)
    for marker, client in FAMILIES:
        if marker in vendor:
            return Guess(client, exact=False)
    return None
