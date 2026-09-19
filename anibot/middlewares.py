"""Мидлварь: прокидывает зависимости, регистрирует юзера, отсекает забаненных."""

from __future__ import annotations

import contextlib
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, Update, User

from . import texts as t
from .config import Config
from .db import Database
from .userbot import Userbot


def unwrap(event: TelegramObject) -> TelegramObject:
    """Достаёт настоящее событие из Update.

    Мидлварь висит на dp.update, поэтому сюда прилетает Update, а не Message.
    Без этого не получится ответить пользователю.
    """
    if isinstance(event, Update):
        with contextlib.suppress(Exception):
            return event.event
    return event


class Deps(BaseMiddleware):
    def __init__(self, db: Database, cfg: Config, userbot: Userbot):
        self.db = db
        self.cfg = cfg
        self.userbot = userbot

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data["db"] = self.db
        data["cfg"] = self.cfg
        data["userbot"] = self.userbot

        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            await self.db.touch_user(user.id, user.username, user.full_name)
            data["is_admin"] = self.cfg.is_admin(user.id)

            if await self.db.is_banned(user.id) and not self.cfg.is_admin(user.id):
                await self._reject(unwrap(event))
                return None

        return await handler(event, data)

    @staticmethod
    async def _reject(event: TelegramObject) -> None:
        with contextlib.suppress(TelegramAPIError):
            if isinstance(event, Message):
                await event.answer(t.BANNED)
            elif isinstance(event, CallbackQuery):
                await event.answer(t.BANNED, show_alert=True)
