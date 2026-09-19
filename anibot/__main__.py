"""Точка входа: python -m anibot"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config, handlers
from .db import Database
from .middlewares import Deps
from .userbot import Userbot

log = logging.getLogger("anibot")

COMMANDS = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="help", description="❓ Как пользоваться"),
    BotCommand(command="id", description="🆔 Мой ID"),
]


async def seed_settings(db: Database, cfg: config.Config) -> None:
    """Переносит значения из env в базу при первом запуске."""
    defaults = {
        "free_episodes": str(cfg.free_episodes),
        "protect_content": str(int(cfg.protect_content)),
        "autodelete": str(cfg.autodelete),
    }
    for key, value in defaults.items():
        if not await db.get_setting(key):
            await db.set_setting(key, value)
    for code, (_label, _days, stars) in config.DEFAULT_PLANS.items():
        if not await db.get_setting(f"price_{code}"):
            await db.set_setting(f"price_{code}", str(stars))


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    cfg = config.load()
    problems = cfg.check()
    if any("BOT_TOKEN" in p for p in problems):
        for problem in problems:
            log.error("Конфигурация: %s", problem)
        sys.exit(1)
    for problem in problems:
        log.warning("Конфигурация: %s", problem)

    db = Database(cfg.db_path)
    await db.connect()
    await seed_settings(db, cfg)
    log.info("База: %s", cfg.db_path)

    userbot = Userbot(cfg.api_id, cfg.api_hash, cfg.session)
    if userbot.configured:
        await userbot.client()
    else:
        log.warning("Юзербот не настроен — большие файлы заливать будет нечем")

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(Deps(db, cfg, userbot))
    handlers.setup(dp)

    me = await bot.get_me()
    log.info("Бот @%s запущен. Админы: %s", me.username, sorted(cfg.admins) or "—")
    if cfg.storage_channel:
        log.info("Хранилище: %s", cfg.storage_channel)

    try:
        await bot.set_my_commands(COMMANDS)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await userbot.stop()
        await db.close()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлен")


if __name__ == "__main__":
    main()
