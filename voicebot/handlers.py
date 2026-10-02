"""Прислали голосовое или кружок — отвечаем, с какого устройства запись."""

from __future__ import annotations

import asyncio
import html
import logging

import aiohttp
from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandStart
from aiogram.types import Message

from . import mp4, opus
from .clients import identify

log = logging.getLogger(__name__)

MAX_DOWNLOAD = 20 * 1024 * 1024  # больше Bot API ботам не отдаёт
OGG_MIME = {"audio/ogg", "audio/opus", "application/ogg"}
OGG_EXT = (".ogg", ".oga", ".opus")
MP4_MIME = {"video/mp4", "video/quicktime", "application/mp4", "audio/mp4"}
MP4_EXT = (".mp4", ".mov", ".m4v", ".m4a")

# Понятные подписи для тегов, которые достаёт инспектор MP4.
MP4_LABELS = {
    "Compressor": "Кодировщик видео",
    "©too": "Кодировщик (©too)",
    "©swr": "Софт",
    "©enc": "Кодировщик",
}

HELLO = (
    "Перешли мне голосовое или кружок — скажу, с какого устройства запись.\n\n"
    "У голосовых смотрю поле Vendor: каждый клиент Telegram пишет туда своё. "
    "Кружки (видео) устроены иначе — по ним пока показываю, что лежит внутри файла."
)
NOT_OPUS = "Это не Ogg/Opus — строки Vendor внутри нет. Пришли обычное голосовое."
NOT_MP4 = "Не похоже на MP4 — не смог разобрать. Пришли кружок или видео."
TOO_BIG = "Файл больше 20 МБ — Telegram не даёт ботам скачивать такие."
DOWNLOAD_FAILED = "Не получилось скачать файл, попробуй ещё раз."

router = Router(name="voice")
router.message.filter(F.chat.type == "private")


def _code(value: str) -> str:
    # строка из чужого файла: прячем непечатаемое и экранируем, чтобы Telegram её принял
    printable = "".join(c if c.isprintable() else "�" for c in value[:200])
    return f"<code>{html.escape(printable)}</code>"


def verdict(vendor: str) -> str:
    """Ответ по голосовому: клиент из строки Vendor."""
    shown = _code(vendor) if vendor.strip() else "пустая строка"
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


def mp4_report(info: mp4.Mp4Info) -> str:
    """Ответ по кружку/видео: не угадываем клиент, а показываем, что внутри."""
    lines = [
        "🎥 Это кружок или видео (MP4). Клиент по нему я пока не определяю: "
        "трюк с Vendor держится на строке из Ogg/Opus, а у MP4 её нет. "
        "Модель телефона по файлу тоже не узнать.",
    ]
    rows: list[tuple[str, str]] = []
    if info.brands:
        rows.append(("Бренды", ", ".join(info.brands)))
    if info.codecs:
        rows.append(("Кодеки", ", ".join(info.codecs)))
    for key, value in info.tags.items():
        rows.append((MP4_LABELS.get(key, key), value))
    if rows:
        lines.append("\nЧто внутри:")
        lines += [f"• {html.escape(label)}: {_code(value)}" for label, value in rows]
    else:
        lines.append("\nНичего опознаваемого внутри не нашлось.")
    lines.append(
        "\nПришли такие же кружки с разных телефонов — сравню, отличается ли "
        "что-то. Если да, соберём таблицу, как для голосовых."
    )
    return "\n".join(lines)


def _kind(message: Message) -> tuple[str, object] | None:
    """Что прислали: ('opus'|'mp4', объект-файл) или None, если не наше."""
    if message.voice:
        return "opus", message.voice
    if message.video_note:
        return "mp4", message.video_note
    if message.video:
        return "mp4", message.video
    media = message.audio or message.document
    if media is None:
        return None
    name = (getattr(media, "file_name", "") or "").lower()
    mime = getattr(media, "mime_type", "") or ""
    if mime in OGG_MIME or name.endswith(OGG_EXT):
        return "opus", media
    if mime in MP4_MIME or name.endswith(MP4_EXT):
        return "mp4", media
    return None


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(HELLO)


@router.message(F.voice | F.audio | F.video_note | F.video | F.document)
async def on_media(message: Message, bot: Bot) -> None:
    what = _kind(message)
    if what is None:
        await message.answer(HELLO)
        return
    kind, media = what
    if (getattr(media, "file_size", 0) or 0) > MAX_DOWNLOAD:
        await message.reply(TOO_BIG)
        return

    try:
        file = await bot.download(media)
    except (TelegramAPIError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("Не скачался файл: %s", e)
        await message.reply(DOWNLOAD_FAILED)
        return
    data = file.read()

    if kind == "opus":
        try:
            vendor = opus.read_vendor(data)
        except opus.NotOpus as e:
            log.info("Не Opus: %s", e)
            await message.reply(NOT_OPUS)
            return
        log.info("Vendor: %r", vendor)
        await message.reply(verdict(vendor), parse_mode=ParseMode.HTML)
        return

    try:
        info = mp4.inspect(data)
    except mp4.NotMp4 as e:
        log.info("Не MP4: %s", e)
        await message.reply(NOT_MP4)
        return
    log.info("MP4: brands=%s codecs=%s tags=%s", info.brands, info.codecs, info.tags)
    await message.reply(mp4_report(info), parse_mode=ParseMode.HTML)


@router.message()
async def other(message: Message) -> None:
    await message.answer(HELLO)
