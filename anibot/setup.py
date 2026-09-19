"""Интерактивная настройка: python -m anibot.setup

Логинит юзербота (нужен код из Telegram), создаёт канал-хранилище,
выдаёт боту права админа и дописывает SESSION и STORAGE_CHANNEL в env-файл.
"""

from __future__ import annotations

import asyncio
import sys
from getpass import getpass
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from . import config
from .userbot import BOT_RIGHTS, to_bot_id

C_OK = "\033[1;32m"
C_ERR = "\033[1;31m"
C_ASK = "\033[1;36m"
C_DIM = "\033[2m"
C_OFF = "\033[0m"


def say(text: str) -> None:
    print(f"{C_OK}==>{C_OFF} {text}")


def fail(text: str) -> None:
    print(f"{C_ERR} !!{C_OFF} {text}", file=sys.stderr)


def ask(text: str) -> str:
    return input(f"{C_ASK} ?{C_OFF} {text}: ").strip()


def banner() -> None:
    print(
        f"""{C_OK}
  ╭───────────────────────────────────────────╮
  │        anime-bot · настройка               │
  ╰───────────────────────────────────────────╯{C_OFF}"""
    )


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Обновляет KEY=value в env-файле, не трогая остальное."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


async def bot_username(token: str) -> str:
    """Спрашивает у Bot API, как зовут бота — чтобы позвать его в канал."""
    from aiogram import Bot

    bot = Bot(token)
    try:
        me = await bot.get_me()
        return me.username or ""
    finally:
        await bot.session.close()


async def login(cfg: config.Config) -> str:
    """Возвращает строку сессии: либо существующую, либо после логина."""
    if cfg.session:
        client = TelegramClient(StringSession(cfg.session), cfg.api_id, cfg.api_hash)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            say(f"Сессия уже есть: {me.first_name} (@{me.username or '—'})")
            await client.disconnect()
            return cfg.session
        await client.disconnect()
        fail("Старая сессия не годится — логинимся заново")

    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()

    phone = ask("Телефон аккаунта-юзербота (в формате +7...)")
    await client.send_code_request(phone)
    code = ask("Код из Telegram")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = getpass(f"{C_ASK} ?{C_OFF} Пароль двухфакторки: ")
        await client.sign_in(password=password)

    me = await client.get_me()
    session = client.session.save()
    say(f"Вошли как {me.first_name} (@{me.username or '—'})")
    await client.disconnect()
    return session


async def make_channel(cfg: config.Config, session: str, username: str) -> int:
    from telethon.tl.functions.channels import (
        CreateChannelRequest,
        EditAdminRequest,
        InviteToChannelRequest,
    )

    client = TelegramClient(StringSession(session), cfg.api_id, cfg.api_hash)
    await client.connect()
    try:
        result = await client(
            CreateChannelRequest(
                title=cfg.channel_title,
                about="Хранилище серий. Не удаляй сообщения — на них ссылается бот.",
                megagroup=False,
            )
        )
        channel = result.chats[0]
        say(f"Канал создан: {cfg.channel_title}")

        try:
            await client(InviteToChannelRequest(channel, [username]))
        except Exception:  # noqa: BLE001 — бывает, что бот уже внутри
            pass
        await client(
            EditAdminRequest(
                channel=channel, user_id=username, admin_rights=BOT_RIGHTS, rank="bot"
            )
        )
        say(f"Бот @{username} назначен админом канала")
        return to_bot_id(channel.id)
    finally:
        await client.disconnect()


async def run() -> None:
    banner()
    cfg = config.load()

    if not cfg.bot_token:
        fail("BOT_TOKEN не задан")
        sys.exit(1)
    if not cfg.api_id or not cfg.api_hash:
        fail("API_ID / API_HASH не заданы — возьми их на my.telegram.org")
        sys.exit(1)

    username = await bot_username(cfg.bot_token)
    if not username:
        fail("Не удалось узнать имя бота — проверь BOT_TOKEN")
        sys.exit(1)
    say(f"Бот: @{username}")

    session = await login(cfg)

    channel_id = cfg.storage_channel
    if channel_id:
        say(f"Канал уже настроен: {channel_id}")
        if ask("Создать новый канал? (y/N)").lower() in {"y", "yes", "д", "да"}:
            channel_id = await make_channel(cfg, session, username)
    else:
        channel_id = await make_channel(cfg, session, username)

    write_env(cfg.env_path, {"SESSION": session, "STORAGE_CHANNEL": str(channel_id)})
    say(f"Записано в {cfg.env_path}")

    print(
        f"""
{C_OK}  Готово.{C_OFF}

  Хранилище: {C_ASK}{channel_id}{C_OFF}
  {C_DIM}Заливай серии в этот канал с подписью:{C_OFF}
      Название: Моё Аниме
      Сезон: 1
      Серия: 7
      Озвучка: Studio Band

  {C_DIM}Запуск:{C_OFF} systemctl restart anime-bot
"""
    )


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print()
        fail("Отменено")
        sys.exit(1)


if __name__ == "__main__":
    main()
