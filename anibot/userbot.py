"""Юзербот на Telethon.

Зачем он нужен, если есть обычный бот:

* Bot API умеет заливать только 50 МБ (2 ГБ со своим сервером), а аккаунт —
  2 ГБ, с Premium 4 ГБ. Серии в 1080p/4K заливает именно юзербот.
* Он же создаёт канал-хранилище и выдаёт боту права админа.
* Он запасной канал выдачи, если у бота что-то не сложилось.

Выдача по умолчанию идёт через бота: copy_message из канала ничего не
перезаливает и не имеет лимита по размеру — даже для файла на 4 ГБ.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    EditAdminRequest,
    InviteToChannelRequest,
)
from telethon.tl.types import ChatAdminRights

log = logging.getLogger("anibot.userbot")

BOT_RIGHTS = ChatAdminRights(
    post_messages=True,
    edit_messages=True,
    delete_messages=True,
    invite_users=True,
    pin_messages=True,
    change_info=False,
    ban_users=False,
    add_admins=False,
    manage_call=False,
)


def to_bot_id(channel_id: int) -> int:
    """ID канала в формате Bot API (-100...)."""
    raw = abs(int(channel_id))
    return int(f"-100{raw}") if not str(channel_id).startswith("-100") else int(channel_id)


class Userbot:
    """Тонкая обёртка над Telethon. Подключается лениво, переживает обрывы."""

    def __init__(self, api_id: int, api_hash: str, session: str):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self._client: Optional[TelegramClient] = None
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.api_id and self.api_hash and self.session)

    async def client(self) -> Optional[TelegramClient]:
        """Возвращает подключённый клиент или None, если юзербот не настроен."""
        if not self.configured:
            return None
        async with self._lock:
            if self._client is not None and self._client.is_connected():
                return self._client
            client = TelegramClient(
                StringSession(self.session), self.api_id, self.api_hash,
                connection_retries=5, retry_delay=2,
            )
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    log.error("Сессия юзербота недействительна — перезапусти python -m anibot.setup")
                    await client.disconnect()
                    return None
            except Exception as exc:  # noqa: BLE001 — на старте важно не упасть
                log.error("Юзербот не подключился: %s", exc)
                return None
            self._client = client
            me = await client.get_me()
            log.info("Юзербот подключён: %s", getattr(me, "username", None) or me.id)
            return client

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    # ---------- канал-хранилище ----------

    async def create_channel(self, title: str, bot_username: str) -> Optional[int]:
        """Создаёт приватный канал и делает бота его админом. Возвращает -100 id."""
        client = await self.client()
        if client is None:
            return None
        result = await client(
            CreateChannelRequest(
                title=title,
                about="Хранилище серий. Не удаляй сообщения — на них ссылается бот.",
                megagroup=False,
            )
        )
        channel = result.chats[0]
        await self.grant_bot(channel, bot_username)
        return to_bot_id(channel.id)

    async def grant_bot(self, channel, bot_username: str) -> bool:
        """Добавляет бота в канал администратором."""
        client = await self.client()
        if client is None:
            return False
        username = bot_username.lstrip("@")
        try:
            await client(InviteToChannelRequest(channel, [username]))
        except Exception as exc:  # noqa: BLE001 — часто уже участник
            log.debug("InviteToChannel: %s", exc)
        try:
            await client(
                EditAdminRequest(
                    channel=channel, user_id=username, admin_rights=BOT_RIGHTS, rank="bot"
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("Не выдал боту права админа: %s", exc)
            return False

    # ---------- заливка ----------

    async def upload(
        self, channel_id: int, path: str | Path, caption: str
    ) -> Optional[int]:
        """Заливает файл с диска сервера в канал. Возвращает message_id."""
        client = await self.client()
        if client is None:
            return None
        path = Path(path)
        if not path.exists():
            log.error("Файла нет: %s", path)
            return None
        entity = await client.get_entity(channel_id)
        message = await client.send_file(
            entity,
            str(path),
            caption=caption[:1024],
            supports_streaming=True,
            force_document=False,
        )
        return int(message.id)

    # ---------- выдача ----------

    async def deliver(self, channel_id: int, message_id: int, user_id: int) -> bool:
        """Пересылает серию пользователю от лица аккаунта.

        Сработает только если аккаунт уже «видел» пользователя — у MTProto
        нет доступа к произвольному user_id без access_hash. Поэтому это
        запасной путь, а основной — copy_message ботом.
        """
        client = await self.client()
        if client is None:
            return False
        try:
            source = await client.get_entity(channel_id)
            target = await client.get_entity(user_id)
            await client.forward_messages(target, message_id, source)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Юзербот не доставил серию %s -> %s: %s", message_id, user_id, exc)
            return False

    async def check_channel(self, channel_id: int) -> bool:
        """Проверяет, что канал на месте и доступен."""
        client = await self.client()
        if client is None:
            return False
        try:
            await client.get_entity(channel_id)
            return True
        except Exception:  # noqa: BLE001
            return False
