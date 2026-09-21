"""Точка входа: python -m anibot"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from . import config, handlers, notify
from .db import Database
from .middlewares import Deps
from .userbot import Userbot

log = logging.getLogger("anibot")

COMMANDS = [
    BotCommand(command="start", description="🏠 Главное меню"),
    BotCommand(command="help", description="❓ Как пользоваться"),
    BotCommand(command="support", description="💬 Написать в поддержку"),
    BotCommand(command="id", description="🆔 Мой ID"),
]


async def seed_settings(db: Database, cfg: config.Config) -> None:
    """Переносит значения из env в базу при первом запуске."""
    defaults = {
        "trial_enabled": str(int(cfg.trial_enabled)),
        "trial_days": str(cfg.trial_days),
        "protect_content": str(int(cfg.protect_content)),
        "autodelete": str(cfg.autodelete),
    }
    for key, value in defaults.items():
        if not await db.get_setting(key):
            await db.set_setting(key, value)
    for code, (_label, _days, stars) in config.DEFAULT_PLANS.items():
        if not await db.get_setting(f"price_{code}"):
            await db.set_setting(f"price_{code}", str(stars))


async def apply_saved_chats(db: Database, cfg: config.Config) -> None:
    """Каналы, назначенные из самого бота, важнее того, что в env.

    Так их можно выдать боту кнопкой, не редактируя файл и не перезапуская
    сервис руками.
    """
    saved = await db.get_int_setting("storage_channel", 0)
    if saved:
        cfg.storage_channel = saved
    group = await db.get_int_setting("service_group", 0)
    if group:
        cfg.service_group = group
        for purpose, key in (
            ("suggestions", "chat_suggestions"),
            ("payments", "chat_payments"),
            ("stats", "chat_stats"),
            ("logs", "chat_logs"),
            ("support", "chat_support"),
        ):
            raw = await db.get_setting(key)
            if not raw:
                continue
            chat, _, thread = raw.partition(":")
            with contextlib.suppress(ValueError):
                cfg.notify[purpose] = (int(chat), int(thread) if thread else None)


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
    await apply_saved_chats(db, cfg)
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

    # ошибки уровня ERROR уходят в служебный чат логов
    logging.getLogger("anibot").addHandler(
        notify.ErrorReporter(bot, cfg, asyncio.get_running_loop())
    )

    await notify.report_startup(bot, cfg, db)
    heartbeat = asyncio.create_task(notify.heartbeat_loop(db))

    try:
        await bot.set_my_commands([])  # никаких команд в меню
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        await notify.report_shutdown(bot, cfg)
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
