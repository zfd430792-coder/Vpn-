"""Интерактивная настройка: python -m anibot.setup

Логинит юзербота (по QR — на российские номера коды часто не доходят),
создаёт канал-хранилище и закрытую служебную группу с темами под
предложения, платежи, статистику и логи, выдаёт боту права и дописывает
всё это в env-файл.
"""

from __future__ import annotations

import asyncio
import io
import sys
from getpass import getpass
from pathlib import Path

import qrcode
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

# назначение -> (ключ в env, имя темы)
TOPICS = [
    ("suggestions", "LOG_SUGGESTIONS", "💡 Предложения"),
    ("payments", "LOG_PAYMENTS", "💰 Платежи"),
    ("stats", "LOG_STATS", "📊 Статистика"),
    ("logs", "LOG_ERRORS", "🛠 Логи и ошибки"),
]


def say(text: str) -> None:
    print(f"{C_OK}==>{C_OFF} {text}")


def fail(text: str) -> None:
    print(f"{C_ERR} !!{C_OFF} {text}", file=sys.stderr)


def ask(text: str) -> str:
    return input(f"{C_ASK} ?{C_OFF} {text}: ").strip()


def yes(text: str) -> bool:
    return ask(f"{text} (y/N)").lower() in {"y", "yes", "д", "да"}


def banner() -> None:
    print(
        f"""{C_OK}
  ╭───────────────────────────────────────────╮
  │        anime-bot · настройка               │
  ╰───────────────────────────────────────────╯{C_OFF}"""
    )


def show_qr(url: str) -> None:
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    buf = io.StringIO()
    code.print_ascii(out=buf)
    print(buf.getvalue())


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
    from aiogram import Bot

    bot = Bot(token)
    try:
        me = await bot.get_me()
        return me.username or ""
    finally:
        await bot.session.close()


# ---------------------------------------------------------------- вход


async def login_by_qr(client: TelegramClient) -> bool:
    """Вход сканированием QR. Возвращает False, если не получилось."""
    print(
        f"""
  {C_ASK}Вход по QR-коду{C_OFF}
  {C_DIM}На телефоне: Telegram → Настройки → Устройства →
  Подключить устройство — и наведи камеру на код ниже.{C_OFF}
"""
    )
    qr = await client.qr_login()
    for attempt in range(1, 6):
        show_qr(qr.url)
        say(f"жду сканирования… (попытка {attempt} из 5, код живёт ~1 минуту)")
        try:
            await qr.wait(timeout=60)
            return True
        except asyncio.TimeoutError:
            say("код истёк, рисую новый")
            await qr.recreate()
        except SessionPasswordNeededError:
            password = getpass(f"{C_ASK} ?{C_OFF} Пароль двухфакторки: ")
            await client.sign_in(password=password)
            return True
    fail("QR так и не отсканировали")
    return False


async def login_by_phone(client: TelegramClient) -> bool:
    phone = ask("Телефон аккаунта (в формате +7...)")
    await client.send_code_request(phone)
    code = ask("Код из Telegram")
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        password = getpass(f"{C_ASK} ?{C_OFF} Пароль двухфакторки: ")
        await client.sign_in(password=password)
    return True


async def login(cfg: config.Config) -> str:
    """Возвращает строку сессии: либо существующую, либо после входа."""
    if cfg.session:
        client = TelegramClient(StringSession(cfg.session), cfg.api_id, cfg.api_hash)
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            say(f"Сессия уже есть: {me.first_name} (@{me.username or '—'})")
            await client.disconnect()
            return cfg.session
        await client.disconnect()
        fail("Старая сессия не годится — входим заново")

    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.connect()
    try:
        ok = False
        if yes("Войти по QR-коду? (рекомендуется: коды по SMS часто не доходят)"):
            ok = await login_by_qr(client)
        if not ok:
            say("пробуем по номеру телефона")
            ok = await login_by_phone(client)
        if not ok:
            fail("Войти не удалось")
            sys.exit(1)

        me = await client.get_me()
        say(f"Вошли как {me.first_name} (@{me.username or '—'})")
        return client.session.save()
    finally:
        await client.disconnect()


# ------------------------------------------------------------ создание


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
        say(f"Канал-хранилище создан: {cfg.channel_title}")
        try:
            await client(InviteToChannelRequest(channel, [username]))
        except Exception:  # noqa: BLE001 — бывает, что бот уже внутри
            pass
        await client(
            EditAdminRequest(
                channel=channel, user_id=username, admin_rights=BOT_RIGHTS, rank="bot"
            )
        )
        say(f"Бот @{username} — админ хранилища")
        return to_bot_id(channel.id)
    finally:
        await client.disconnect()


async def make_service_chats(
    cfg: config.Config, session: str, username: str, token: str
) -> dict[str, str]:
    """Закрытая служебная группа с темами. Возвращает строки для env."""
    from aiogram import Bot
    from aiogram.exceptions import TelegramAPIError

    from .userbot import Userbot

    userbot = Userbot(cfg.api_id, cfg.api_hash, session)
    group_id, forum = await userbot.create_service_group("Anime Bot · служебная", username)
    await userbot.stop()

    if group_id is None:
        fail("Служебную группу создать не вышло — уведомления пойдут в личку админам")
        return {}

    say(f"Служебная группа создана ({group_id})")
    # поддержка заводит тему на каждого обратившегося сама, поэтому тут
    # хранится только группа, без номера темы
    env: dict[str, str] = {"SERVICE_GROUP": str(group_id), "LOG_SUPPORT": str(group_id)}

    if not forum:
        say("темы Telegram не дал — всё служебное пойдёт в общий чат группы")
        for _purpose, key, _name in TOPICS:
            env[key] = str(group_id)
        return env

    bot = Bot(token)
    try:
        for _purpose, key, name in TOPICS:
            created = False
            for attempt in range(4):
                try:
                    topic = await bot.create_forum_topic(chat_id=group_id, name=name)
                    env[key] = f"{group_id}:{topic.message_thread_id}"
                    say(f"тема «{name}» готова")
                    created = True
                    break
                except TelegramAPIError as exc:
                    # боту нужно время, чтобы права доехали
                    if attempt == 3:
                        fail(f"тему «{name}» не создал: {exc}")
                    await asyncio.sleep(1.5)
            if not created:
                env[key] = str(group_id)
    finally:
        await bot.session.close()
    return env


# ---------------------------------------------------------------- ход


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
    updates: dict[str, str] = {"SESSION": session}

    channel_id = cfg.storage_channel
    if channel_id:
        say(f"Хранилище уже настроено: {channel_id}")
        if yes("Создать новое хранилище?"):
            channel_id = await make_channel(cfg, session, username)
    else:
        channel_id = await make_channel(cfg, session, username)
    updates["STORAGE_CHANNEL"] = str(channel_id)

    if cfg.service_group:
        say(f"Служебная группа уже есть: {cfg.service_group}")
        if yes("Создать служебную группу заново?"):
            updates.update(await make_service_chats(cfg, session, username, cfg.bot_token))
    else:
        updates.update(await make_service_chats(cfg, session, username, cfg.bot_token))

    write_env(cfg.env_path, updates)
    say(f"Записано в {cfg.env_path}")

    print(
        f"""
{C_OK}  Готово.{C_OFF}

  Хранилище:        {C_ASK}{channel_id}{C_OFF}
  Служебная группа: {C_ASK}{updates.get('SERVICE_GROUP', '—')}{C_OFF}
  {C_DIM}Темы: предложения, платежи, статистика, логи.
  Обращения в поддержку заводят свою тему на каждого человека.{C_OFF}

  {C_DIM}Заливай серии в хранилище с подписью:{C_OFF}
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
