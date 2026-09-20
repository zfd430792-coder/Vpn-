"""Поддержка: переписка с админом прямо в боте.

Чтобы не путаться, кто есть кто, каждому обратившемуся заводится своя
тема в служебной группе. Админ отвечает прямо в теме — бот передаёт ответ
человеку. Вся переписка с одним человеком всегда в одном месте.

Если тем нет (Telegram их не дал или служебная группа не настроена),
обращения уходят админам в личку, а ответить можно командой
/reply <id> <текст> — она работает в любом случае.
"""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import notify
from .. import texts as t
from ..config import Config
from ..db import Database
from .common import show

log = logging.getLogger("anibot.support")
router = Router(name="support")


class SupportFSM(StatesGroup):
    waiting = State()


def _who(user) -> str:
    name = user.full_name or "без имени"
    handle = f"@{user.username}" if user.username else "без ника"
    return f"{name} · {handle} · <code>{user.id}</code>"


async def _ensure_thread(bot: Bot, cfg: Config, db: Database, user) -> int:
    """Тема пользователя в служебной группе. 0 — тем нет, пойдём в личку."""
    chat_id, _thread = cfg.target(notify.SUPPORT)
    if not chat_id:
        chat_id = cfg.service_group
    if not chat_id:
        return 0

    ticket = await db.ensure_ticket(user.id)
    if ticket["thread_id"]:
        return int(ticket["thread_id"])

    title = f"💬 {user.full_name or user.id}"[:60]
    try:
        topic = await bot.create_forum_topic(chat_id=chat_id, name=title)
    except TelegramAPIError as exc:
        log.warning("Тему для обращения не завёл: %s", exc)
        return 0

    thread_id = topic.message_thread_id
    await db.set_ticket_thread(user.id, thread_id)
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(
            chat_id,
            f"💬 <b>Новое обращение</b>\n{_who(user)}\n\n"
            "<i>Отвечай прямо в этой теме — ответ уйдёт человеку.\n"
            "/close — закрыть обращение.</i>",
            message_thread_id=thread_id,
        )
    return thread_id


# ---------- сторона пользователя ----------


@router.callback_query(kb.Nav.filter(F.to == "support"))
async def nav_support(call: CallbackQuery, state: FSMContext):
    await state.set_state(SupportFSM.waiting)
    await show(call, t.SUPPORT_START, kb.back_to())


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    await state.set_state(SupportFSM.waiting)
    await message.answer(t.SUPPORT_START, reply_markup=kb.back_to())


@router.message(SupportFSM.waiting, F.chat.type == "private", ~F.text.startswith("/"))
async def from_user(
    message: Message, db: Database, cfg: Config, state: FSMContext, bot: Bot
):
    user = message.from_user
    chat_id, _thread = cfg.target(notify.SUPPORT)
    if not chat_id:
        chat_id = cfg.service_group

    thread_id = await _ensure_thread(bot, cfg, db, user)
    await db.touch_ticket(user.id)

    delivered = False
    if chat_id:
        try:
            await bot.copy_message(
                chat_id=chat_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
                message_thread_id=thread_id or None,
            )
            delivered = True
        except TelegramAPIError as exc:
            log.warning("Обращение не ушло в группу: %s", exc)

    if not delivered:
        # запасной путь: админам в личку, отвечать через /reply
        for admin_id in cfg.admins:
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(admin_id, f"💬 <b>Вопрос</b>\n{_who(user)}")
                await bot.copy_message(admin_id, message.chat.id, message.message_id)
                delivered = True

    if not delivered:
        await message.answer(t.SUPPORT_OFF, reply_markup=kb.back_to())
        return

    await message.answer(t.SUPPORT_SENT, reply_markup=kb.back_to())


# ---------- сторона админа ----------


@router.message(Command("close"), F.chat.type.in_({"group", "supergroup"}))
async def close_ticket(message: Message, db: Database, cfg: Config, bot: Bot):
    if not cfg.is_admin(message.from_user.id):
        return
    thread_id = message.message_thread_id or 0
    ticket = await db.ticket_by_thread(thread_id) if thread_id else None
    if ticket is None:
        await message.reply("Это не тема обращения.")
        return
    await db.close_ticket(ticket["user_id"])
    await message.reply("✅ Закрыл.")
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(ticket["user_id"], t.SUPPORT_CLOSED)


@router.message(Command("reply"), F.chat.type == "private")
async def cmd_reply(message: Message, db: Database, cfg: Config, bot: Bot):
    """Ответ без тем: /reply <id> <текст>."""
    if not cfg.is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: <code>/reply ID текст</code>")
        return
    user_id, text = int(parts[1]), parts[2]
    try:
        await bot.send_message(user_id, f"{t.SUPPORT_REPLY}\n{text}")
    except TelegramAPIError as exc:
        await message.answer(f"⚠️ Не доставил: <code>{exc}</code>")
        return
    await db.touch_ticket(user_id)
    await message.answer("✅ Ответ отправлен.")


@router.message(
    StateFilter(None),
    F.chat.type.in_({"group", "supergroup"}),
    F.message_thread_id.is_not(None),
    ~F.text.startswith("/"),
)
async def from_admin(message: Message, db: Database, cfg: Config, bot: Bot):
    """Сообщение в теме обращения — передаём человеку."""
    ticket = await db.ticket_by_thread(message.message_thread_id)
    if ticket is None:
        return
    if not cfg.is_admin(message.from_user.id):
        return

    user_id = int(ticket["user_id"])
    try:
        if message.text:
            await bot.send_message(user_id, f"{t.SUPPORT_REPLY}\n{message.text}")
        else:
            await bot.send_message(user_id, t.SUPPORT_REPLY)
            await bot.copy_message(user_id, message.chat.id, message.message_id)
    except TelegramAPIError as exc:
        await message.reply(f"⚠️ Не доставил: <code>{exc}</code>")
        return

    await db.touch_ticket(user_id)
    with contextlib.suppress(TelegramAPIError):
        await message.react([{"type": "emoji", "emoji": "👌"}])
