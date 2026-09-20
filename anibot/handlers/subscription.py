"""Подписка: тестовый доступ, промокоды, оплата в Telegram Stars."""

from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from .. import keyboards as kb
from .. import notify
from .. import service
from .. import texts as t
from ..config import Config
from ..db import Database
from .common import show

log = logging.getLogger("anibot.subs")
router = Router(name="subscription")

STARS = "XTR"


class PromoFSM(StatesGroup):
    waiting = State()


@router.callback_query(kb.Nav.filter(F.to == "subs"))
async def nav_subs(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext):
    await state.clear()
    user_id = call.from_user.id
    until = await db.sub_until(user_id)
    plans = await service.plans(db, user_id)
    trial_ok = await service.can_take_trial(db, cfg, user_id)
    _enabled, trial_days = await service.trial_settings(db, cfg)
    percent, code = await db.get_discount(user_id)

    text = t.SUBS_HEADER.format(sub=t.human_until(until))
    if percent:
        text += f"\n\n🎟 Скидка <b>{percent}%</b> по коду <code>{code}</code> — цены уже с ней."

    await show(call, text, kb.subscription(plans, until > 0, trial_ok, trial_days))


# ---------- тестовая подписка ----------


@router.callback_query(kb.Nav.filter(F.to == "trial"))
async def nav_trial(call: CallbackQuery, db: Database, cfg: Config):
    user_id = call.from_user.id
    if not await service.can_take_trial(db, cfg, user_id):
        enabled, _days = await service.trial_settings(db, cfg)
        if not enabled:
            await call.answer("Тестовая подписка сейчас выключена", show_alert=True)
        elif await db.has_sub(user_id):
            await call.answer("У тебя и так есть подписка", show_alert=True)
        else:
            await call.answer("Тестовую подписку ты уже использовал", show_alert=True)
        return

    until = await service.give_trial(db, cfg, user_id)
    await call.answer("Включил 🎁")
    await show(
        call,
        t.TRIAL_TAKEN.format(until=t.human_until(until)),
        kb.main_menu(cfg.is_admin(user_id)),
    )
    await notify.send(
        call.bot,
        cfg,
        notify.PAYMENTS,
        f"🎁 Тестовая подписка: <code>{user_id}</code> @{call.from_user.username or '—'}",
    )


# ---------- промокоды ----------


@router.callback_query(kb.Nav.filter(F.to == "promo"))
async def nav_promo(call: CallbackQuery, state: FSMContext):
    await state.set_state(PromoFSM.waiting)
    await show(call, t.PROMO_ASK, kb.back_to())


@router.message(PromoFSM.waiting, F.text & ~F.text.startswith("/"))
async def redeem(message: Message, db: Database, cfg: Config, state: FSMContext):
    code = (message.text or "").strip().upper()
    user_id = message.from_user.id
    promo = await db.get_promo(code)

    if promo is None:
        await message.answer(t.PROMO_BAD, reply_markup=kb.back_to())
        return
    if promo["expires_at"] and promo["expires_at"] < int(time.time()):
        await message.answer(t.PROMO_EXPIRED, reply_markup=kb.back_to())
        return
    if promo["max_uses"] and promo["used"] >= promo["max_uses"]:
        await message.answer(t.PROMO_LIMIT, reply_markup=kb.back_to())
        return
    if await db.promo_used_by(promo["id"], user_id):
        await message.answer(t.PROMO_SPENT, reply_markup=kb.back_to())
        return

    await state.clear()
    await db.use_promo(promo["id"], user_id)

    if promo["kind"] == "sub":
        until = await db.grant_sub(user_id, int(promo["days"]))
        await message.answer(
            t.PROMO_SUB_OK.format(until=t.human_until(until)),
            reply_markup=kb.main_menu(cfg.is_admin(user_id)),
        )
        note = f"на {promo['days']} дн."
    else:
        await db.set_discount(user_id, int(promo["percent"]), code)
        await message.answer(
            t.PROMO_DISCOUNT_OK.format(percent=promo["percent"]),
            reply_markup=kb.paywall(),
        )
        note = f"скидка {promo['percent']}%"

    await notify.send(
        message.bot,
        cfg,
        notify.PAYMENTS,
        f"🎟 Промокод <code>{code}</code> ({note})\n"
        f"использовал <code>{user_id}</code> @{message.from_user.username or '—'}",
    )


# ---------- оплата ----------


@router.callback_query(kb.Pay.filter())
async def start_payment(call: CallbackQuery, callback_data: kb.Pay, db: Database):
    plans = await service.plans(db, call.from_user.id)
    plan = plans.get(callback_data.plan)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return
    label, days, stars = plan
    percent, _code = await db.get_discount(call.from_user.id)

    description = (
        f"Полный доступ к каталогу на {days} дней."
        if days <= 3650
        else "Полный доступ к каталогу навсегда."
    )
    if percent:
        description += f" Скидка {percent}% учтена."

    await call.answer()
    await call.message.answer_invoice(
        title=f"Подписка · {label}",
        description=description,
        payload=f"sub:{callback_data.plan}",
        provider_token="",  # для Stars пустой
        currency=STARS,
        prices=[LabeledPrice(label=label, amount=stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    # Отказывать не за что: товар цифровой и всегда доступен.
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, db: Database, cfg: Config):
    payment = message.successful_payment
    payload = payment.invoice_payload or ""
    code = payload.split(":", 1)[1] if ":" in payload else "month"

    plans = await service.plans(db)
    label, days, _price = plans.get(code, ("Месяц", 30, 0))

    user_id = message.from_user.id
    until = await db.grant_sub(user_id, days)
    await db.add_payment(
        user_id, code, payment.total_amount, payment.telegram_payment_charge_id or ""
    )
    # скидка одноразовая — сгорает после оплаты
    await db.clear_discount(user_id)

    await message.answer(
        t.PAID_OK.format(plan=label, until=t.human_until(until)),
        reply_markup=kb.main_menu(cfg.is_admin(user_id)),
    )

    log.info("Оплата: user=%s plan=%s stars=%s", user_id, code, payment.total_amount)
    await notify.send(
        message.bot,
        cfg,
        notify.PAYMENTS,
        f"💰 <b>Оплата</b>\n"
        f"<code>{user_id}</code> @{message.from_user.username or '—'}\n"
        f"Тариф: <b>{label}</b> · <b>{payment.total_amount}</b> ⭐\n"
        f"Подписка до: {t.human_until(until)}",
    )
