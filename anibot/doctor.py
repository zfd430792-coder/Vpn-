"""Диагностика установки: python -m anibot.doctor

Проверяет всё, что обычно ломается: токен, ID админа, данные юзербота,
сессию, канал-хранилище и права бота в нём. На каждую поломку печатает,
что именно чинить.
"""

from __future__ import annotations

import asyncio
import sys

from . import config

OK = "\033[1;32m✅\033[0m"
NO = "\033[1;31m❌\033[0m"
WARN = "\033[1;33m⚠️ \033[0m"
DIM = "\033[2m"
OFF = "\033[0m"

problems: list[str] = []


def good(text: str) -> None:
    print(f"  {OK} {text}")


def bad(text: str, fix: str) -> None:
    print(f"  {NO} {text}")
    print(f"     {DIM}→ {fix}{OFF}")
    problems.append(text)


def warn(text: str, note: str = "") -> None:
    print(f"  {WARN}{text}")
    if note:
        print(f"     {DIM}{note}{OFF}")


def mask(value: str) -> str:
    if len(value) <= 12:
        return value[:3] + "***"
    return f"{value[:10]}…{value[-4:]}"


async def check_bot(cfg: config.Config) -> tuple[object | None, int]:
    """Проверяет токен. Возвращает (bot, bot_id) или (None, 0)."""
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError

    if not cfg.bot_token:
        bad("BOT_TOKEN не задан", f"впиши его в {cfg.env_path} и перезапусти сервис")
        return None, 0

    bot = Bot(cfg.bot_token)
    try:
        me = await bot.get_me()
        good(f"токен рабочий — бот @{me.username} ({mask(cfg.bot_token)})")
        return bot, me.id
    except TelegramAPIError as exc:
        await bot.session.close()
        bad(
            f"Telegram не принял токен: {exc}",
            "возьми новый у @BotFather (/mybots → API Token) и впиши "
            f"в {cfg.env_path}, строка BOT_TOKEN=",
        )
        return None, 0
    except Exception as exc:  # noqa: BLE001 — сеть, DNS и прочее
        await bot.session.close()
        bad(f"не достучался до Telegram: {exc}", "проверь сеть и доступность api.telegram.org")
        return None, 0


async def check_channel(bot, bot_id: int, cfg: config.Config) -> None:
    from aiogram.exceptions import TelegramAPIError

    if not cfg.storage_channel:
        bad(
            "хранилище не назначено — боту неоткуда брать серии",
            "добавь бота администратором в свой канал: он сам напишет тебе "
            "и предложит кнопку «Сделать хранилищем». Либо, если настроен "
            "юзербот: python -m anibot.setup",
        )
        return
    try:
        chat = await bot.get_chat(cfg.storage_channel)
        good(f"канал-хранилище на месте: {chat.title} ({cfg.storage_channel})")
    except TelegramAPIError as exc:
        bad(
            f"канал {cfg.storage_channel} недоступен боту: {exc}",
            "проверь, что канал не удалён и бот из него не вышел; "
            "заново: python -m anibot.setup",
        )
        return
    try:
        member = await bot.get_chat_member(cfg.storage_channel, bot_id)
        if member.status in {"administrator", "creator"}:
            good("бот — администратор канала, выдача работать будет")
        else:
            bad(
                f"бот в канале не админ (статус: {member.status})",
                "выдай ему админку в канале вручную или перезапусти python -m anibot.setup",
            )
    except TelegramAPIError as exc:
        warn(f"не смог проверить права бота в канале: {exc}")


async def check_userbot(cfg: config.Config) -> None:
    if not cfg.api_id and not cfg.api_hash:
        warn(
            "юзербот не настроен",
            "это нормально, если заливаешь тайтлы вручную со своего аккаунта: "
            "бот отдаёт серии сам. Юзербот нужен только для заливки с диска "
            "сервера и автосоздания каналов.",
        )
        return
    if not cfg.api_id or not cfg.api_hash:
        bad(
            "задано только одно из API_ID / API_HASH",
            "впиши оба или оставь оба пустыми",
        )
        return
    good(f"API_ID задан ({cfg.api_id}), API_HASH задан")

    if not cfg.session:
        bad("SESSION пустая — юзербот не залогинен", "запусти: python -m anibot.setup")
        return

    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(cfg.session), cfg.api_id, cfg.api_hash)
    try:
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            good(f"юзербот залогинен: {me.first_name} (@{me.username or '—'})")
        else:
            bad("сессия юзербота недействительна", "перелогинься: python -m anibot.setup")
    except Exception as exc:  # noqa: BLE001
        bad(f"юзербот не подключился: {exc}", "перелогинься: python -m anibot.setup")
    finally:
        if client.is_connected():
            await client.disconnect()


async def check_db(cfg: config.Config) -> None:
    from .db import Database

    db = Database(cfg.db_path)
    try:
        await db.connect()
        stats = await db.stats()
        good(
            f"база на месте: {stats['anime']} тайтлов, {stats['episodes']} серий, "
            f"{stats['users']} юзеров"
        )
        if not stats["episodes"]:
            warn(
                "серий пока нет",
                "залей видео в канал-хранилище с подписью "
                "«Название / Сезон: / Серия: / Озвучка:»",
            )
    except Exception as exc:  # noqa: BLE001
        bad(f"база недоступна: {exc}", f"проверь права на {cfg.db_path.parent}")
    finally:
        await db.close()


async def run() -> None:
    print("\n\033[1;36m  Проверка установки anime-bot\033[0m")
    print("  " + "─" * 44)

    cfg = config.load()
    print(f"\n  {DIM}конфиг: {cfg.env_path}{OFF}\n")

    if cfg.admins:
        good(f"админы: {', '.join(str(i) for i in sorted(cfg.admins))}")
    else:
        bad(
            "ADMINS не задан — админку будет некому открыть",
            f"впиши свой числовой ID (узнать у @userinfobot) в {cfg.env_path}, строка ADMINS=",
        )

    bot, bot_id = await check_bot(cfg)
    if bot is not None:
        await check_channel(bot, bot_id, cfg)
        await bot.session.close()

    await check_userbot(cfg)
    await check_db(cfg)

    print("\n  " + "─" * 44)
    if problems:
        print(f"\033[1;31m  Поломок: {len(problems)}\033[0m — что чинить, написано выше.\n")
        sys.exit(1)
    print("\033[1;32m  Всё в порядке.\033[0m\n")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
