"""Точка входа: BOT_TOKEN=... python -m voicebot"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError

from .handlers import router

log = logging.getLogger("voicebot")


async def run(token: str) -> None:
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(Bot(token))  # сам пишет в лог, какой бот запущен


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        log.error("Нет BOT_TOKEN. Возьми токен у @BotFather и передай переменной окружения.")
        sys.exit(1)
    try:
        asyncio.run(run(token))
    except TelegramUnauthorizedError:
        log.error("Telegram не принял BOT_TOKEN — проверь токен у @BotFather.")
        sys.exit(1)
    except TelegramNetworkError as e:
        log.error("Нет связи с Telegram: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
