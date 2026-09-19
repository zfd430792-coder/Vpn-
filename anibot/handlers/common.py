"""Мелкие помощники для хендлеров."""

from __future__ import annotations

import contextlib

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardMarkup


async def show(call: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Перерисовывает сообщение, из которого пришёл callback.

    Если сообщение с медиа (текст там не редактируется) — шлёт новое.
    """
    message = call.message
    if message is None:
        await call.answer()
        return
    try:
        if message.text is not None:
            await message.edit_text(text, reply_markup=markup)
        else:
            await message.answer(text, reply_markup=markup)
    except TelegramAPIError:
        with contextlib.suppress(TelegramAPIError):
            await message.answer(text, reply_markup=markup)
    await call.answer()


def pages_of(total: int, per_page: int) -> int:
    return max(1, (total + per_page - 1) // per_page)
