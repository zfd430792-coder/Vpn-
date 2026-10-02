"""Прислали голосовое — отвечаем, с какого устройства его записали."""

from __future__ import annotations

import asyncio
import html
import logging

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import Audio, Document, Message

from . import opus
from .clients import identify

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 20 * 1024 * 1024  # больше Bot API ботам не отдаёт
OGG_MIME = {"audio/ogg", "audio/opus", "application/ogg"}
OGG_EXT = (".ogg", ".oga", ".opus")

HELLO = (
    "Перешли мне голосовое — скажу, с какого устройства его записали.\n\n"
    "Смотрю на поле Vendor внутри файла: каждый клиент Telegram пишет туда своё."
)
NOT_OPUS = "Это не Ogg/Opus — строки Vendor внутри нет. Пришли обычное голосовое."
TOO_BIG = "Файл больше 20 МБ — Telegram не даёт ботам скачивать такие."
DOWNLOAD_FAILED = "Не получилось скачать файл, попробуй ещё раз."

router = Router(name="voice")
router.message.filter(F.chat.type == "private")


def verdict(vendor: str) -> str:
    # строка из чужого файла: прячем непечатаемое и экранируем, чтобы Telegram её принял
    printable = "".join(c if c.isprintable() else "�" for c in vendor[:200])
    shown = f"<code>{html.escape(printable)}</code>" if vendor.strip() else "пустая строка"
    guess = identify(vendor)
    if guess is None:
        return f"🤷 Не знаю такой клиент\nVendor: {shown}"
    client = html.escape(guess.client)
    if guess.exact:
        return f"✅ <b>{client}</b>\nVendor: {shown}"
    return (
        f"🤔 Похоже на <b>{client}</b>\nVendor: {shown}\n\n"
        "Точно такой строки в таблице нет — видимо, у клиента сменилась версия."
    )


def _is_ogg(media: Audio | Document) -> bool:
    """Голосовое, присланное файлом, а не как голосовое."""
    name = (media.file_name or "").lower()
    return media.mime_type in OGG_MIME or name.endswith(OGG_EXT)


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(HELLO)


@router.message(F.voice | F.audio | F.document)
async def on_voice(message: Message, bot: Bot) -> None:
    media = message.voice or message.audio or message.document
    if message.voice is None and not _is_ogg(media):
        await message.answer(HELLO)
        return
    if (media.file_size or 0) > MAX_DOWNLOAD:
        await message.reply(TOO_BIG)
        return

    try:
        file = await bot.download(media)
    except (TelegramAPIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Не скачался файл: %s", e)
        await message.reply(DOWNLOAD_FAILED)
        return

    try:
        vendor = opus.read_vendor(file.read())
    except opus.NotOpus as e:
        log.info("Не Opus: %s", e)
        await message.reply(NOT_OPUS)
        return

    log.info("Vendor: %r", vendor)
    await message.reply(verdict(vendor), parse_mode=ParseMode.HTML)


@router.message()
async def other(message: Message) -> None:
    await message.answer(HELLO)
