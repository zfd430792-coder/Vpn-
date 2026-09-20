"""Предложения тайтлов с подсчётом голосов.

Люди пишут одно и то же по-разному, поэтому голос не создаёт новую строку
вслепую: сначала ищется совпадение среди уже предложенного. Уверенное —
засчитывается молча, спорное — бот показывает варианты и спрашивает.
Подтверждённый вариант запоминается псевдонимом, и в следующий раз то же
сокращение попадёт в цель сразу.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import notify
from .. import suggest
from .. import texts as t
from ..config import Config
from ..db import Database
from ..search import normalize
from .common import show

router = Router(name="suggestions")

PENDING = "suggest_text"


class SuggestFSM(StatesGroup):
    waiting = State()


async def _candidates(db: Database) -> list[suggest.Candidate]:
    rows = await db.open_suggestions()
    out = []
    for row in rows:
        aliases = (row["aliases"] or "").split("|")
        out.append(
            suggest.Candidate(
                id=row["id"],
                title=row["title"],
                norm=row["norm"],
                votes=row["votes"] or 0,
                aliases=[a for a in aliases if a],
            )
        )
    return out


async def _board(db: Database, user_id: int) -> tuple[str, object]:
    rows = await db.top_suggestions(15)
    if not rows:
        return t.SUGGEST_EMPTY + "\n\n" + t.SUGGEST_ASK, kb.back_to()
    voted = set()
    for row in rows:
        if not await db.vote_exists(row["id"], user_id):
            continue
        voted.add(row["id"])
    text = (
        f"💡 <b>Что просят добавить</b>\n{t.SEP}\n"
        "Тап по строке — плюс голос. Или напиши своё название."
    )
    return text, kb.suggestions(rows, voted)


@router.callback_query(kb.Nav.filter(F.to == "suggest"))
async def nav_suggest(call: CallbackQuery, db: Database, state: FSMContext):
    await state.set_state(SuggestFSM.waiting)
    text, markup = await _board(db, call.from_user.id)
    await show(call, text + "\n\n" + t.SUGGEST_ASK, markup)


async def _also_watch(db: Database, user_id: int, row, suggestion_id: int) -> None:
    """Проголосовал — значит хочет посмотреть. Подписываем на появление.

    Отдельной кнопки не нужно: голос и есть заявка. Отписаться можно,
    просто не голосуя, а уведомление приходит один раз.
    """
    title = row["title"] if row is not None else ""
    if not title:
        return
    await db.add_watch(user_id, title, normalize(title), suggestion_id)


@router.callback_query(kb.Nav.filter(F.to == "sgvote"))
async def nav_vote(call: CallbackQuery, callback_data: kb.Nav, db: Database):
    row = await db.get_suggestion(callback_data.i)
    if row is None:
        await call.answer("Предложение уже закрыли", show_alert=True)
        return
    added = await db.vote(callback_data.i, call.from_user.id)
    await _also_watch(db, call.from_user.id, row, callback_data.i)
    votes = await db.votes_of(callback_data.i)
    await call.answer(
        f"Голос учтён · {votes} · сообщу, когда появится"
        if added
        else f"Ты уже голосовал · {votes}"
    )
    text, markup = await _board(db, call.from_user.id)
    await show(call, text, markup)


@router.callback_query(kb.Nav.filter(F.to == "sgpick"))
async def nav_pick(
    call: CallbackQuery, callback_data: kb.Nav, db: Database, cfg: Config, state: FSMContext
):
    """Человек подтвердил, какой тайтл имел в виду — запоминаем написание."""
    data = await state.get_data()
    raw = (data.get(PENDING) or "").strip()
    row = await db.get_suggestion(callback_data.i)
    if row is None:
        await call.answer("Предложение уже закрыли", show_alert=True)
        return

    if raw:
        await db.add_alias(callback_data.i, normalize(raw))
    added = await db.vote(callback_data.i, call.from_user.id)
    await _also_watch(db, call.from_user.id, row, callback_data.i)
    votes = await db.votes_of(callback_data.i)
    await state.update_data({PENDING: ""})

    text = (
        t.SUGGEST_VOTED.format(title=row["title"], votes=votes)
        if added
        else t.SUGGEST_ALREADY.format(title=row["title"], votes=votes)
    )
    board, markup = await _board(db, call.from_user.id)
    await show(call, text + "\n\n" + board, markup)


@router.callback_query(kb.Nav.filter(F.to == "sgnew"))
async def nav_new(
    call: CallbackQuery, db: Database, cfg: Config, state: FSMContext
):
    data = await state.get_data()
    raw = (data.get(PENDING) or "").strip()
    if not raw:
        await call.answer("Название потерялось — напиши заново", show_alert=True)
        return
    suggestion_id = await db.add_suggestion(raw, normalize(raw))
    await db.vote(suggestion_id, call.from_user.id)
    await db.add_watch(call.from_user.id, raw, normalize(raw), suggestion_id)
    await state.update_data({PENDING: ""})
    await _announce(call.bot, cfg, db, raw, suggestion_id, call.from_user.id)

    board, markup = await _board(db, call.from_user.id)
    await show(call, t.SUGGEST_NEW.format(title=raw) + "\n\n" + board, markup)


@router.message(
    SuggestFSM.waiting, F.chat.type == "private", F.text & ~F.text.startswith("/")
)
async def got_text(
    message: Message, db: Database, cfg: Config, state: FSMContext
):
    raw = (message.text or "").strip()
    if len(raw) < 2:
        await message.answer("Слишком коротко — напиши название подробнее.")
        return

    # уже известное написание — засчитываем сразу
    known = await db.find_by_alias(normalize(raw))
    if known is not None:
        row = await db.get_suggestion(known)
        added = await db.vote(known, message.from_user.id)
        await _also_watch(db, message.from_user.id, row, known)
        votes = await db.votes_of(known)
        text = (
            t.SUGGEST_VOTED.format(title=row["title"], votes=votes)
            if added
            else t.SUGGEST_ALREADY.format(title=row["title"], votes=votes)
        )
        board, markup = await _board(db, message.from_user.id)
        await message.answer(text + "\n\n" + board, reply_markup=markup)
        return

    best, alternatives = suggest.match(raw, await _candidates(db))

    if best is not None:
        await db.add_alias(best.candidate.id, normalize(raw))
        added = await db.vote(best.candidate.id, message.from_user.id)
        await db.add_watch(
            message.from_user.id, best.candidate.title,
            normalize(best.candidate.title), best.candidate.id,
        )
        votes = await db.votes_of(best.candidate.id)
        text = (
            t.SUGGEST_VOTED.format(title=best.candidate.title, votes=votes)
            if added
            else t.SUGGEST_ALREADY.format(title=best.candidate.title, votes=votes)
        )
        board, markup = await _board(db, message.from_user.id)
        await message.answer(text + "\n\n" + board, reply_markup=markup)
        return

    if alternatives:
        await state.update_data({PENDING: raw})
        await message.answer(
            t.SUGGEST_WHICH + f"\n\nТы написал: <b>{raw}</b>",
            reply_markup=kb.suggest_which(alternatives, 0),
        )
        return

    suggestion_id = await db.add_suggestion(raw, normalize(raw))
    await db.vote(suggestion_id, message.from_user.id)
    await db.add_watch(message.from_user.id, raw, normalize(raw), suggestion_id)
    await _announce(message.bot, cfg, db, raw, suggestion_id, message.from_user.id)
    board, markup = await _board(db, message.from_user.id)
    await message.answer(
        t.SUGGEST_NEW.format(title=raw) + "\n\n" + board, reply_markup=markup
    )


async def _announce(bot, cfg: Config, db: Database, title: str, sid: int, user_id: int) -> None:
    await notify.send(
        bot,
        cfg,
        notify.SUGGESTIONS,
        f"💡 <b>Новое предложение</b>\n"
        f"<b>{title}</b>\n"
        f"от <code>{user_id}</code> · id предложения: <code>{sid}</code>",
    )
