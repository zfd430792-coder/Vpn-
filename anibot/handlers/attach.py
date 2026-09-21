"""Назначение каналов из самого бота.

Если заливать тайтлы вручную со своего аккаунта, юзербот не нужен вообще:
бот отдаёт серии сам, копируя сообщения из канала. Остаётся только сказать
ему, какой канал считать хранилищем, — и это делается кнопкой.

Как только бота добавляют администратором в канал или группу, он пишет
админам: вот чат, вот его id, назначить хранилищем или служебной группой?
Выбор сохраняется в базе и применяется сразу, без правки env и без
перезапуска сервиса.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters.callback_data import CallbackData
from aiogram.types import (CallbackQuery, ChatMemberUpdated, InlineKeyboardButton,
                           InlineKeyboardMarkup)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .. import texts as t
from ..config import Config
from ..db import Database

log = logging.getLogger("anibot.attach")
router = Router(name="attach")

ADMIN_STATUSES = {"administrator", "creator"}

# назначение -> (ключ настройки, имя темы)
TOPICS = [
    ("suggestions", "chat_suggestions", "💡 Предложения"),
    ("payments", "chat_payments", "💰 Платежи"),
    ("stats", "chat_stats", "📊 Статистика"),
    ("logs", "chat_logs", "🛠 Логи и ошибки"),
]


class Attach(CallbackData, prefix="at"):
    role: str  # storage | service | skip
    chat: int


def _btn(text: str, role: str, chat_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Attach(role=role, chat=chat_id).pack())


def _keyboard(chat_id: int, is_channel: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if is_channel:
        kb.row(_btn("📦 Сделать хранилищем", "storage", chat_id))
    else:
        kb.row(_btn("🛠 Сделать служебной группой", "service", chat_id))
    kb.row(_btn("✖️ Ничего не делать", "skip", chat_id))
    return kb.as_markup()


@router.my_chat_member()
async def on_added(event: ChatMemberUpdated, db: Database, cfg: Config, bot: Bot):
    """Бота куда-то добавили — если админом, предлагаем назначить роль."""
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    if new_status not in ADMIN_STATUSES or old_status in ADMIN_STATUSES:
        return
    if event.chat.type == "private":
        return

    is_channel = event.chat.type == "channel"
    kind = "канал" if is_channel else "группу"
    known = (
        event.chat.id == cfg.storage_channel
        or event.chat.id == cfg.service_group
    )
    note = "\n\n<i>Этот чат уже назначен.</i>" if known else ""

    text = (
        f"🔗 <b>Меня добавили админом в {kind}</b>\n{t.SEP}\n"
        f"Название: <b>{event.chat.title or '—'}</b>\n"
        f"ID: <code>{event.chat.id}</code>{note}\n\n"
        "Что с ним делать?"
    )
    for admin_id in cfg.admins:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(
                admin_id, text, reply_markup=_keyboard(event.chat.id, is_channel)
            )
    log.info("Добавлен админом: %s (%s)", event.chat.title, event.chat.id)


@router.callback_query(Attach.filter(F.role == "skip"))
async def skip(call: CallbackQuery, cfg: Config):
    if not cfg.is_admin(call.from_user.id):
        await call.answer("Недоступно", show_alert=True)
        return
    await call.answer("Ок, не трогаю")
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text("✖️ Оставил как есть.")


@router.callback_query(Attach.filter(F.role == "storage"))
async def as_storage(
    call: CallbackQuery, callback_data: Attach, db: Database, cfg: Config, bot: Bot
):
    if not cfg.is_admin(call.from_user.id):
        await call.answer("Недоступно", show_alert=True)
        return

    chat_id = callback_data.chat
    try:
        chat = await bot.get_chat(chat_id)
    except TelegramAPIError as exc:
        await call.answer("Канал недоступен", show_alert=True)
        log.warning("Канал %s недоступен: %s", chat_id, exc)
        return

    await db.set_setting("storage_channel", str(chat_id))
    cfg.storage_channel = chat_id
    await call.answer("Хранилище назначено")
    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text(
            f"📦 <b>Хранилище: {chat.title}</b>\n{t.SEP}\n"
            f"ID: <code>{chat_id}</code>\n\n"
            "Заливай сюда серии с подписью:\n"
            "<pre>Название: Моё Аниме\n"
            "Сезон: 1\n"
            "Серия: 7\n"
            "Озвучка: Studio Band\n"
            "Качество: 4к</pre>\n"
            "Я подхвачу сам. Перезапускать ничего не надо."
        )


@router.callback_query(Attach.filter(F.role == "service"))
async def as_service(
    call: CallbackQuery, callback_data: Attach, db: Database, cfg: Config, bot: Bot
):
    if not cfg.is_admin(call.from_user.id):
        await call.answer("Недоступно", show_alert=True)
        return

    chat_id = callback_data.chat
    await db.set_setting("service_group", str(chat_id))
    cfg.service_group = chat_id
    await call.answer("Назначаю…")

    made: list[str] = []
    for purpose, key, name in TOPICS:
        try:
            topic = await bot.create_forum_topic(chat_id=chat_id, name=name)
            value = f"{chat_id}:{topic.message_thread_id}"
            cfg.notify[purpose] = (chat_id, topic.message_thread_id)
            made.append(name)
        except TelegramAPIError as exc:
            # темы могут быть не включены — тогда всё идёт в общий чат
            log.info("Тему «%s» не создал: %s", name, exc)
            value = str(chat_id)
            cfg.notify[purpose] = (chat_id, None)
        await db.set_setting(key, value)

    await db.set_setting("chat_support", str(chat_id))
    cfg.notify["support"] = (chat_id, None)

    lines = [f"🛠 <b>Служебная группа назначена</b>\n{t.SEP}", f"ID: <code>{chat_id}</code>"]
    if made:
        lines.append("Заведены темы: " + ", ".join(made))
    else:
        lines.append(
            "Темы не включились — всё служебное пойдёт в общий чат группы.\n"
            "<i>Включить можно в настройках группы, пункт «Темы».</i>"
        )
    lines.append("\nОбращения в поддержку заведут свою тему на каждого человека.")

    with contextlib.suppress(TelegramAPIError):
        await call.message.edit_text("\n".join(lines))
