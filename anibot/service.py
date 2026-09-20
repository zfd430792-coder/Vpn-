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


async def trial_settings(db: Database, cfg: Config) -> tuple[bool, int]:
    """(включена ли тестовая подписка, на сколько дней)"""
    enabled = bool(await db.get_int_setting("trial_enabled", int(cfg.trial_enabled)))
    days = await db.get_int_setting("trial_days", cfg.trial_days)
    return enabled, max(1, days)


async def can_take_trial(db: Database, cfg: Config, user_id: int) -> bool:
    """Доступна ли человеку тестовая подписка прямо сейчас."""
    enabled, _days = await trial_settings(db, cfg)
    if not enabled:
        return False
    if await db.has_sub(user_id):
        return False
    return not await db.trial_used(user_id)


async def give_trial(db: Database, cfg: Config, user_id: int) -> int:
    """Выдаёт тестовую подписку. Возвращает, до какого времени она活 действует."""
    _enabled, days = await trial_settings(db, cfg)
    await db.mark_trial_used(user_id)
    return await db.grant_sub(user_id, days)


async def has_access(db: Database, cfg: Config, user_id: int, episode_id: int) -> bool:
    """Пускать ли пользователя к серии.

    Бесплатных серий больше нет: доступ даёт подписка, а познакомиться
    с ботом можно через тестовую подписку — её человек включает сам.
    """
    if cfg.is_admin(user_id):
        return True
    return await db.has_sub(user_id)


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


async def status_line(db: Database, cfg: Config, user_id: int) -> str:
    """Строчка о доступе для профиля и пейволла."""
    if cfg.is_admin(user_id):
        return t.QUOTA_UNLIMITED
    if await db.has_sub(user_id):
        return t.QUOTA_UNLIMITED
    if await can_take_trial(db, cfg, user_id):
        _enabled, days = await trial_settings(db, cfg)
        return t.TRIAL_OFFER.format(days=days)
    if await db.trial_used(user_id):
        return t.TRIAL_SPENT
    return t.QUOTA_OVER


async def plans(db: Database, user_id: int | None = None) -> dict[str, tuple[str, int, int]]:
    """Тарифы с ценами из настроек. Если передан user_id — с его скидкой."""
    from .config import DEFAULT_PLANS

    percent = 0
    if user_id is not None:
        percent, _promo = await db.get_discount(user_id)

    out: dict[str, tuple[str, int, int]] = {}
    for code, (label, days, stars) in DEFAULT_PLANS.items():
        price = await db.get_int_setting(f"price_{code}", stars)
        if percent:
            price = max(1, round(price * (100 - percent) / 100))
        out[code] = (label, days, price)
    return out
