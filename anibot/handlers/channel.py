"""Приём серий из канала-хранилища.

Админ просто заливает видео в канал и пишет подпись — бот (он там админ)
видит пост, разбирает подпись и заводит серию в каталоге.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from .. import parser
from .. import search as se
from ..config import Config
from ..db import Database

log = logging.getLogger("anibot.channel")
router = Router(name="channel")


async def _notify(bot: Bot, cfg: Config, text: str) -> None:
    for admin_id in cfg.admins:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, text)


async def _ingest(message: Message, db: Database, cfg: Config, bot: Bot) -> None:
    if not cfg.storage_channel or message.chat.id != cfg.storage_channel:
        return

    media = message.video or message.document or message.animation
    if media is None:
        return

    parsed = parser.parse(message.caption or "")
    if parsed is None:
        await _notify(
            bot,
            cfg,
            "⚠️ <b>Не разобрал подпись</b> к посту "
            f"<code>{message.message_id}</code> в хранилище.\n\n"
            "Нужен один из форматов:\n"
            "<code>Название | сезон | серия | озвучка</code>\n"
            "<code>Название S01E07 [Озвучка]</code>\n"
            "или построчно <code>Название:/Сезон:/Серия:/Озвучка:</code>\n\n"
            "Поправь подпись — я подхвачу сам.",
        )
        return

    anime_id = await db.add_anime(parsed.title, se.normalize(parsed.title))
    await db.add_episode(
        anime_id=anime_id,
        season=parsed.season,
        number=parsed.episode,
        dub=parsed.dub,
        message_id=message.message_id,
        file_size=getattr(media, "file_size", 0) or 0,
        duration=getattr(media, "duration", 0) or 0,
    )
    log.info("Из канала: %s", parsed)
    await _notify(bot, cfg, f"✅ В каталоге: <b>{parsed.title}</b>\nS{parsed.season} · E{parsed.episode} · {parsed.dub}")


@router.channel_post(F.video | F.document | F.animation)
async def on_channel_post(message: Message, db: Database, cfg: Config, bot: Bot):
    await _ingest(message, db, cfg, bot)


@router.edited_channel_post(F.video | F.document | F.animation)
async def on_channel_post_edited(message: Message, db: Database, cfg: Config, bot: Bot):
    """Подпись поправили — перечитываем."""
    await _ingest(message, db, cfg, bot)
