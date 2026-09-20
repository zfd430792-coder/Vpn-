"""Выдача серии: проверка доступа, отправка, листание серий."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from .. import keyboards as kb
from .. import service
from .. import texts as t
from ..config import Config
from ..db import Database, Episode
from ..userbot import Userbot
from .common import show

router = Router(name="watch")


async def send_episode(
    call: CallbackQuery,
    ep: Episode,
    db: Database,
    cfg: Config,
    userbot: Userbot,
    **_,
) -> None:
    """Общая точка выдачи — её зовут и каталог, и плеер."""
    user_id = call.from_user.id

    if not await service.has_access(db, cfg, user_id, ep.id):
        status = await service.status_line(db, cfg, user_id)
        trial_ok = await service.can_take_trial(db, cfg, user_id)
        _enabled, trial_days = await service.trial_settings(db, cfg)
        await show(
            call,
            t.PAYWALL.format(status=status),
            kb.paywall(trial_ok, trial_days),
        )
        return

    anime = await db.get_anime(ep.anime_id)
    if anime is None:
        await show(call, "🤷 Тайтл не найден.", kb.back_to())
        return

    await call.answer("📤 Отправляю…")
    ok = await service.deliver(call.bot, userbot, db, cfg, user_id, anime, ep)
    if not ok:
        await call.message.answer(
            "⚠️ Не получилось отправить серию.\n"
            "Похоже, файл пропал из хранилища — напиши админу.",
            reply_markup=kb.back_to(),
        )


@router.callback_query(kb.Nav.filter(F.to == "watch"))
async def nav_watch(call: CallbackQuery, callback_data: kb.Nav, db: Database, **kwargs):
    ep = await db.get_episode(callback_data.i)
    if ep is None:
        await show(call, "📭 Серия не найдена.", kb.back_to())
        return
    await send_episode(call, ep, db=db, **kwargs)


@router.callback_query(kb.Nav.filter(F.to == "step"))
async def nav_step(call: CallbackQuery, callback_data: kb.Nav, db: Database, **kwargs):
    """Соседняя серия — стрелки в плеере."""
    current = await db.get_episode(callback_data.i)
    if current is None:
        await show(call, "📭 Серия не найдена.", kb.back_to())
        return
    target = await db.neighbour(current, callback_data.e)
    if target is None:
        await call.answer("Это крайняя серия сезона", show_alert=True)
        return
    await send_episode(call, target, db=db, **kwargs)
