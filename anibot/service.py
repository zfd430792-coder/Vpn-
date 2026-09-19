"""Проверка доступа и собственно выдача серии."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import keyboards as kb
from . import texts as t
from .config import Config
from .db import Anime, Database, Episode
from .userbot import Userbot

log = logging.getLogger("anibot.service")


async def free_quota(db: Database, cfg: Config) -> int:
    return await db.get_int_setting("free_episodes", cfg.free_episodes)


async def has_access(db: Database, cfg: Config, user_id: int, episode_id: int) -> bool:
    """Пускать ли пользователя к этой серии."""
    if cfg.is_admin(user_id):
        return True
    if await db.has_sub(user_id):
        return True
    quota = await free_quota(db, cfg)
    if quota <= 0:
        return False
    # уже открытую серию не считаем повторно — иначе квота тает от перемоток
    if await db.has_seen(user_id, episode_id):
        return True
    return await db.views_count(user_id) < quota


async def _autodelete(bot: Bot, chat_id: int, message_id: int, minutes: int) -> None:
    await asyncio.sleep(minutes * 60)
    with contextlib.suppress(TelegramAPIError):
        await bot.delete_message(chat_id, message_id)


async def deliver(
    bot: Bot,
    userbot: Userbot,
    db: Database,
    cfg: Config,
    user_id: int,
    anime: Anime,
    ep: Episode,
) -> bool:
    """Отдаёт серию пользователю.

    Основной путь — copy_message из канала: файл не перезаливается,
    ограничения по размеру нет. Если бот не смог — пробует юзербот.
    """
    dub_count = len(await db.dubs(ep.anime_id, ep.season, ep.number))
    has_prev = await db.neighbour(ep, -1) is not None
    has_next = await db.neighbour(ep, +1) is not None

    caption = t.EPISODE_CAPTION.format(
        title=anime.title,
        season=ep.season,
        number=ep.number,
        dub=ep.dub,
        size=t.human_size(ep.file_size),
        duration=t.human_duration(ep.duration),
    )
    protect = bool(await db.get_int_setting("protect_content", int(cfg.protect_content)))
    autodelete = await db.get_int_setting("autodelete", cfg.autodelete)

    sent = None
    if cfg.delivery != "userbot":
        try:
            sent = await bot.copy_message(
                chat_id=user_id,
                from_chat_id=cfg.storage_channel,
                message_id=ep.message_id,
                caption=caption,
                protect_content=protect,
                reply_markup=kb.player(ep, has_prev, has_next, dub_count),
            )
        except TelegramAPIError as exc:
            log.warning("copy_message не прошёл (ep=%s): %s", ep.id, exc)

    if sent is None:
        ok = await userbot.deliver(cfg.storage_channel, ep.message_id, user_id)
        if not ok:
            return False
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(
                user_id, caption, reply_markup=kb.player(ep, has_prev, has_next, dub_count)
            )

    await db.add_view(user_id, ep.id)

    if sent is not None and autodelete > 0:
        asyncio.create_task(_autodelete(bot, user_id, sent.message_id, autodelete))

    return True


async def quota_line(db: Database, cfg: Config, user_id: int) -> str:
    """Строчка про остаток бесплатных серий для профиля."""
    if cfg.is_admin(user_id) or await db.has_sub(user_id):
        return t.QUOTA_UNLIMITED
    quota = await free_quota(db, cfg)
    if quota <= 0:
        return t.QUOTA_OVER
    used = await db.views_count(user_id)
    left = max(0, quota - used)
    if left == 0:
        return t.QUOTA_OVER
    return t.QUOTA_LEFT.format(left=left, total=quota)


async def plans(db: Database) -> dict[str, tuple[str, int, int]]:
    """Тарифы с ценами из настроек (админ меняет их в панели)."""
    from .config import DEFAULT_PLANS

    out: dict[str, tuple[str, int, int]] = {}
    for code, (label, days, stars) in DEFAULT_PLANS.items():
        price = await db.get_int_setting(f"price_{code}", stars)
        out[code] = (label, days, price)
    return out
