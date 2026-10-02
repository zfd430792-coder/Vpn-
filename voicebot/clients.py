"""Какой телефон записал голосовое — по строке Vendor."""

from __future__ import annotations

# Точные строки Vendor. Нашёл новую — допиши сюда.
KNOWN: dict[str, str] = {
    "libopus 1.5.1": "iPhone",
    "libopus unknown-fixed": "Android",
    "Lavf60.16.101": "Telegram Desktop",
    "libopus 1.3.1-fixed": "macOS",
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


def identify(vendor: str) -> str | None:
    vendor = vendor.strip()
    if vendor in KNOWN:
        return KNOWN[vendor]
    for marker, client in FAMILIES:
        if marker in vendor:
            return client
    return None
