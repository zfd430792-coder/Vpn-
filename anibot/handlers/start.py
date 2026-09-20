"""Старт, главное меню, профиль, помощь."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import service
from .. import texts as t
from ..config import Config
from ..db import Database
from .common import show

router = Router(name="start")


async def _greeting(db: Database) -> str:
    return t.GREETING.format(anime=await db.count_anime(), episodes=await db.count_episodes())


@router.message(CommandStart())
async def cmd_start(message: Message, db: Database, state: FSMContext, is_admin: bool = False):
    await state.clear()
    await message.answer(await _greeting(db), reply_markup=kb.main_menu(is_admin))


@router.message(Command("help"))
async def cmd_help(message: Message, is_admin: bool = False):
    await message.answer(t.HELP, reply_markup=kb.main_menu(is_admin))


@router.message(Command("menu"))
async def cmd_menu(message: Message, db: Database, state: FSMContext, is_admin: bool = False):
    await state.clear()
    await message.answer(await _greeting(db), reply_markup=kb.main_menu(is_admin))


@router.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"🆔 Твой ID: <code>{message.from_user.id}</code>")


@router.callback_query(kb.Nav.filter(F.to == "menu"))
async def nav_menu(call: CallbackQuery, db: Database, state: FSMContext, is_admin: bool = False):
    await state.clear()
    await show(call, await _greeting(db), kb.main_menu(is_admin))


@router.callback_query(kb.Nav.filter(F.to == "help"))
async def nav_help(call: CallbackQuery, is_admin: bool = False):
    await show(call, t.HELP, kb.back_to())


@router.callback_query(kb.Nav.filter(F.to == "search"))
async def nav_search(call: CallbackQuery):
    await show(
        call,
        "🔍 <b>Поиск</b>\n\nПросто напиши название аниме сообщением — найду по совпадению.",
        kb.back_to(),
    )


@router.callback_query(kb.Nav.filter(F.to == "profile"))
async def nav_profile(call: CallbackQuery, db: Database, cfg: Config):
    user_id = call.from_user.id
    until = await db.sub_until(user_id)
    text = t.PROFILE.format(
        user_id=user_id,
        sub=t.human_until(until),
        views=await db.views_count(user_id),
        quota=await service.status_line(db, cfg, user_id),
    )
    await show(call, text, kb.back_to())


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()
