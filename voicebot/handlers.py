"""Прислали голосовое — отвечаем одним словом, с какого телефона оно."""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import Message

from . import opus
from .clients import identify

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 20 * 1024 * 1024  # больше Bot API ботам не отдаёт

HELLO = "Перешли голосовое — напишу, с какого телефона."
UNKNOWN = "Неизвестно"
DOWNLOAD_FAILED = "Не скачалось, перешли ещё раз"

router = Router(name="voice")
router.message.filter(F.chat.type == "private")


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(HELLO)


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    voice = message.voice
    if (voice.file_size or 0) > MAX_DOWNLOAD:
        await message.reply(UNKNOWN)
        return

    try:
        file = await bot.download(voice)
    except (TelegramAPIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Не скачался файл: %s", e)
        await message.reply(DOWNLOAD_FAILED)
        return

    try:
        vendor = opus.read_vendor(file.read())
    except opus.NotOpus as e:
        log.info("Не Opus: %s", e)
        await message.reply(UNKNOWN)
        return

    log.info("Vendor: %r", vendor)  # по логу видно строки, которых нет в таблице
    await message.reply(identify(vendor) or UNKNOWN)
