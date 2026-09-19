"""Подписка за Telegram Stars: витрина, счёт, оплата."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from .. import keyboards as kb
from .. import service
from .. import texts as t
from ..config import Config
from ..db import Database
from .common import show

log = logging.getLogger("anibot.subs")
router = Router(name="subscription")

STARS = "XTR"


@router.callback_query(kb.Nav.filter(F.to == "subs"))
async def nav_subs(call: CallbackQuery, db: Database):
    until = await db.sub_until(call.from_user.id)
    plans = await service.plans(db)
    await show(
        call,
        t.SUBS_HEADER.format(sub=t.human_until(until)),
        kb.subscription(plans, until > 0),
    )


@router.callback_query(kb.Pay.filter())
async def start_payment(call: CallbackQuery, callback_data: kb.Pay, db: Database):
    plans = await service.plans(db)
    plan = plans.get(callback_data.plan)
    if plan is None:
        await call.answer("Тариф не найден", show_alert=True)
        return
    label, days, stars = plan

    await call.answer()
    await call.message.answer_invoice(
        title=f"Подписка · {label}",
        description=(
            f"Полный доступ к каталогу на {days} дней."
            if days <= 3650
            else "Полный доступ к каталогу навсегда."
        ),
        payload=f"sub:{callback_data.plan}",
        # для Stars provider_token пустой, валюта XTR
        provider_token="",
        currency=STARS,
        prices=[LabeledPrice(label=label, amount=stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    # Отказывать тут не за что: товар цифровой и всегда доступен.
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_paid(message: Message, db: Database, cfg: Config):
    payment = message.successful_payment
    payload = payment.invoice_payload or ""
    code = payload.split(":", 1)[1] if ":" in payload else "month"

    plans = await service.plans(db)
    label, days, _price = plans.get(code, ("Месяц", 30, 0))

    until = await db.grant_sub(message.from_user.id, days)
    await db.add_payment(
        message.from_user.id,
        code,
        payment.total_amount,
        payment.telegram_payment_charge_id or "",
    )

    await message.answer(
        t.PAID_OK.format(plan=label, until=t.human_until(until)),
        reply_markup=kb.main_menu(cfg.is_admin(message.from_user.id)),
    )

    log.info(
        "Оплата: user=%s plan=%s stars=%s", message.from_user.id, code, payment.total_amount
    )
    for admin_id in cfg.admins:
        try:
            await message.bot.send_message(
                admin_id,
                f"💰 Оплата: <code>{message.from_user.id}</code> "
                f"@{message.from_user.username or '—'}\n"
                f"Тариф: <b>{label}</b> · <b>{payment.total_amount}</b> ⭐",
            )
        except Exception as exc:  # noqa: BLE001 — админ мог не нажать /start
            log.debug("Уведомление админу %s не ушло: %s", admin_id, exc)
