"""Админка: статистика, каталог, заливка серий, рассылка, подписки, настройки."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import secrets

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import parser
from .. import search as se
from .. import service
from .. import texts as t
from ..config import DEFAULT_PLANS, Config
from ..db import Database
from ..userbot import Userbot
from .common import pages_of, show

log = logging.getLogger("anibot.admin")
router = Router(name="admin")


class AdminFSM(StatesGroup):
    broadcast = State()
    grant = State()
    ban = State()
    rename = State()
    set_trial_days = State()
    set_autodelete = State()
    promo_value = State()
    promo_code = State()
    promo_uses = State()
    sugg_merge = State()
    set_price = State()
    up_title = State()
    up_season = State()
    up_episode = State()
    up_dub = State()
    up_quality = State()


def _admin_only(cfg: Config, user_id: int) -> bool:
    return cfg.is_admin(user_id)


async def _panel_text(db: Database) -> str:
    return t.ADMIN_PANEL.format(**await db.stats())


# ---------- вход в панель ----------


@router.message(Command("admin"))
async def cmd_admin(message: Message, db: Database, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, message.from_user.id):
        return
    await state.clear()
    await message.answer(await _panel_text(db), reply_markup=kb.admin_panel())


@router.callback_query(kb.Nav.filter(F.to == "admin"))
async def nav_admin(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        await call.answer("Недоступно", show_alert=True)
        return
    await state.clear()
    await show(call, await _panel_text(db), kb.admin_panel())


@router.callback_query(kb.Adm.filter(F.act == "refresh"))
async def adm_refresh(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        await call.answer("Недоступно", show_alert=True)
        return
    await state.clear()
    await show(call, await _panel_text(db), kb.admin_panel())


@router.callback_query(kb.Adm.filter(F.act == "howto"))
async def adm_howto(call: CallbackQuery, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    await show(call, t.ADMIN_HOWTO, kb.admin_cancel())


# ---------- каталог ----------


@router.callback_query(kb.Adm.filter(F.act == "titles"))
async def adm_titles(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    total = await db.count_anime()
    if not total:
        await show(call, "📭 Каталог пуст.", kb.admin_cancel())
        return
    pages = pages_of(total, kb.PER_PAGE)
    page = max(0, min(callback_data.p, pages - 1))
    items = await db.list_anime(page * kb.PER_PAGE, kb.PER_PAGE)
    await show(
        call,
        f"🎬 <b>Тайтлы</b> ({total})\n{t.SEP}\nВыбери, чтобы отредактировать:",
        kb.admin_titles(items, page, pages),
    )


@router.callback_query(kb.Adm.filter(F.act == "title"))
async def adm_title(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    anime = await db.get_anime(callback_data.arg)
    if anime is None:
        await show(call, "🤷 Тайтл не найден.", kb.admin_cancel())
        return
    seasons, episodes, dubs, quals = await db.anime_summary(anime.id)
    text = (
        f"🎬 <b>{anime.title}</b>\n{t.SEP}\n"
        f"🆔 <code>{anime.id}</code>\n"
        f"📀 Сезонов: <b>{seasons}</b>  ·  🎞 Серий: <b>{episodes}</b>\n"
        f"🎙 Озвучки: {', '.join(dubs) or '—'}\n"
        f"💎 Качество: {', '.join(parser.QUALITY_NAMES.get(q, str(q)) for q in quals) or '—'}"
    )
    await show(call, text, kb.admin_title(anime.id))


@router.callback_query(kb.Adm.filter(F.act == "del_ask"))
async def adm_del_ask(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    anime = await db.get_anime(callback_data.arg)
    if anime is None:
        return
    count = await db.count_episodes(anime.id)
    await show(
        call,
        f"🗑 Удалить <b>{anime.title}</b> и все его серии ({count})?\n\n"
        "Записи уйдут из базы. Файлы в канале останутся на месте.",
        kb.admin_confirm_delete(anime.id),
    )


@router.callback_query(kb.Adm.filter(F.act == "del_yes"))
async def adm_del_yes(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    await db.delete_anime(callback_data.arg)
    await call.answer("Удалено")
    await show(call, await _panel_text(db), kb.admin_panel())


@router.callback_query(kb.Adm.filter(F.act == "rename"))
async def adm_rename(call: CallbackQuery, callback_data: kb.Adm, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.rename)
    await state.update_data(anime_id=callback_data.arg)
    await show(call, "✏️ Пришли новое название одним сообщением:", kb.admin_cancel())


@router.message(AdminFSM.rename)
async def adm_rename_save(message: Message, db: Database, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Название пустое, попробуй ещё раз.")
        return
    data = await state.get_data()
    await db.rename_anime(data["anime_id"], title, se.normalize(title))
    await state.clear()
    await message.answer(f"✅ Теперь это <b>{title}</b>", reply_markup=kb.admin_panel())


# ---------- заливка серии через личку ----------


@router.message(F.video | F.document | F.animation)
async def adm_upload(
    message: Message, db: Database, cfg: Config, state: FSMContext, bot: Bot
):
    """Админ прислал видео в личку — разбираем подпись или спрашиваем по шагам."""
    if not _admin_only(cfg, message.from_user.id):
        return
    if message.chat.type != "private":
        return

    media = message.video or message.document or message.animation
    await state.update_data(
        src_chat=message.chat.id,
        src_msg=message.message_id,
        size=getattr(media, "file_size", 0) or 0,
        duration=getattr(media, "duration", 0) or 0,
    )

    parsed = parser.parse(message.caption or "")
    if parsed:
        await _save_upload(message, db, cfg, state, bot, parsed)
        return

    await state.set_state(AdminFSM.up_title)
    await message.answer(
        "📥 <b>Принял видео.</b>\n"
        f"{t.SEP}\n"
        "Как называется аниме?\n\n"
        "<i>Подсказка: в следующий раз напиши подпись к видео — "
        "<code>Название | сезон | серия | озвучка</code> — и я не буду спрашивать.</i>",
        reply_markup=kb.admin_cancel(),
    )


@router.message(AdminFSM.up_title)
async def adm_up_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title:
        await message.answer("Пустое название, давай ещё раз.")
        return
    await state.update_data(title=title)
    await state.set_state(AdminFSM.up_season)
    await message.answer("📀 Сезон? (числом, или <code>-</code> для первого)")


@router.message(AdminFSM.up_season)
async def adm_up_season(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    season = 1 if raw in {"-", ""} else parser.as_int(raw, 1) or 1
    await state.update_data(season=season)
    await state.set_state(AdminFSM.up_episode)
    await message.answer("🎞 Номер серии?")


@router.message(AdminFSM.up_episode)
async def adm_up_episode(message: Message, state: FSMContext):
    number = parser.as_int(message.text or "")
    if not number:
        await message.answer("Нужно число. Номер серии?")
        return
    await state.update_data(number=number)
    await state.set_state(AdminFSM.up_dub)
    await message.answer("🎙 Озвучка? (например, <code>Studio Band</code>)")


@router.message(AdminFSM.up_dub)
async def adm_up_dub(message: Message, state: FSMContext):
    dub = (message.text or "").strip() or parser.DEFAULT_DUB
    await state.update_data(dub=dub)
    await state.set_state(AdminFSM.up_quality)
    await message.answer(
        "💎 Качество? Пришли <code>1080</code> или <code>4k</code>.\n"
        "<i>Просто Enter или «-» — будет 1080p.</i>"
    )


@router.message(AdminFSM.up_quality)
async def adm_up_quality(
    message: Message, db: Database, cfg: Config, state: FSMContext, bot: Bot
):
    raw = (message.text or "").strip()
    quality, _rest = parser.quality_of(raw) if raw not in {"-", ""} else (
        parser.DEFAULT_QUALITY,
        "",
    )
    data = await state.get_data()
    parsed = parser.Parsed(
        title=data.get("title", "Без названия"),
        season=data.get("season", 1),
        episode=data.get("number", 1),
        dub=data.get("dub", parser.DEFAULT_DUB),
        quality=quality,
    )
    await _save_upload(message, db, cfg, state, bot, parsed)


async def _save_upload(
    message: Message,
    db: Database,
    cfg: Config,
    state: FSMContext,
    bot: Bot,
    parsed: parser.Parsed,
) -> None:
    """Копирует присланное видео в канал-хранилище и пишет строку в базу."""
    data = await state.get_data()
    src_chat = data.get("src_chat", message.chat.id)
    src_msg = data.get("src_msg", message.message_id)

    if not cfg.storage_channel:
        await message.answer("⚠️ STORAGE_CHANNEL не настроен — запусти <code>python -m anibot.setup</code>")
        await state.clear()
        return

    caption = (
        f"Название: {parsed.title}\n"
        f"Сезон: {parsed.season}\n"
        f"Серия: {parsed.episode}\n"
        f"Озвучка: {parsed.dub}\n"
        f"Качество: {parsed.quality_name}"
    )
    try:
        copied = await bot.copy_message(
            chat_id=cfg.storage_channel,
            from_chat_id=src_chat,
            message_id=src_msg,
            caption=caption,
        )
    except TelegramAPIError as exc:
        await message.answer(f"⚠️ Не смог положить в канал: <code>{exc}</code>")
        await state.clear()
        return

    anime_id = await db.add_anime(parsed.title, se.normalize(parsed.title))
    await db.add_episode(
        anime_id=anime_id,
        season=parsed.season,
        number=parsed.episode,
        dub=parsed.dub,
        quality=parsed.quality,
        message_id=copied.message_id,
        file_size=data.get("size", 0),
        duration=data.get("duration", 0),
    )
    await state.clear()
    await message.answer(
        f"✅ <b>Сохранено</b>\n{t.SEP}\n"
        f"🎬 {parsed.title}\n"
        f"📀 Сезон {parsed.season} · 🎞 Серия {parsed.episode}\n"
        f"🎙 {parsed.dub} · 💎 {parsed.quality_name}",
        reply_markup=kb.admin_panel(),
    )


# ---------- рассылка ----------


@router.callback_query(kb.Adm.filter(F.act == "broadcast"))
async def adm_broadcast(call: CallbackQuery, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.broadcast)
    await show(call, "📣 Пришли сообщение — разошлю всем живым пользователям.", kb.admin_cancel())


@router.message(AdminFSM.broadcast)
async def adm_broadcast_send(message: Message, db: Database, state: FSMContext, bot: Bot):
    await state.clear()
    user_ids = await db.all_user_ids()
    status = await message.answer(f"📣 Рассылаю на {len(user_ids)}…")

    sent = failed = 0
    for index, user_id in enumerate(user_ids, 1):
        try:
            await bot.copy_message(user_id, message.chat.id, message.message_id)
            sent += 1
        except TelegramAPIError:
            failed += 1
        if index % 25 == 0:
            await asyncio.sleep(1)  # бережём лимиты Telegram
            with contextlib.suppress(TelegramAPIError):
                await status.edit_text(f"📣 {index}/{len(user_ids)}…")

    await status.edit_text(
        f"📣 <b>Готово</b>\n{t.SEP}\n✅ Доставлено: <b>{sent}</b>\n❌ Не дошло: <b>{failed}</b>",
        reply_markup=kb.admin_panel(),
    )


# ---------- подписки и баны ----------


@router.callback_query(kb.Adm.filter(F.act == "grant"))
async def adm_grant(call: CallbackQuery, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.grant)
    await show(
        call,
        "⭐ Пришли <code>ID дни</code>\n\n"
        "Например: <code>123456789 30</code>\n"
        "Отнять подписку: <code>123456789 0</code>",
        kb.admin_cancel(),
    )


@router.message(AdminFSM.grant)
async def adm_grant_do(message: Message, db: Database, state: FSMContext, bot: Bot):
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: <code>ID дни</code>")
        return
    user_id, days = int(parts[0]), int(parts[1])
    await state.clear()

    if days <= 0:
        await db.revoke_sub(user_id)
        await message.answer(f"🚫 Подписка снята с <code>{user_id}</code>", reply_markup=kb.admin_panel())
        return

    until = await db.grant_sub(user_id, days)
    await message.answer(
        f"✅ <code>{user_id}</code> — подписка на <b>{t.human_until(until)}</b>",
        reply_markup=kb.admin_panel(),
    )
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(
            user_id, f"⭐ Тебе выдали подписку: <b>{t.human_until(until)}</b>. Приятного просмотра!"
        )


@router.callback_query(kb.Adm.filter(F.act == "ban"))
async def adm_ban(call: CallbackQuery, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.ban)
    await show(
        call,
        "🚫 Пришли <code>ID</code> — забаню.\n"
        "Разбанить: <code>ID +</code>",
        kb.admin_cancel(),
    )


@router.message(AdminFSM.ban)
async def adm_ban_do(message: Message, db: Database, state: FSMContext):
    parts = (message.text or "").split()
    if not parts or not parts[0].isdigit():
        await message.answer("Нужен числовой ID.")
        return
    user_id = int(parts[0])
    unban = len(parts) > 1 and parts[1] in {"+", "-", "unban", "разбан"}
    await db.set_banned(user_id, not unban)
    await state.clear()
    verdict = "разбанен" if unban else "забанен"
    await message.answer(f"✅ <code>{user_id}</code> {verdict}", reply_markup=kb.admin_panel())


# ---------- платежи ----------


@router.callback_query(kb.Adm.filter(F.act == "payments"))
async def adm_payments(call: CallbackQuery, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    rows = await db.last_payments(15)
    if not rows:
        await show(call, "💰 Платежей ещё не было.", kb.admin_cancel())
        return
    lines = [f"💰 <b>Последние платежи</b>\n{t.SEP}"]
    for row in rows:
        mark = "↩️" if row["refunded"] else "✅"
        lines.append(
            f"{mark} <code>{row['user_id']}</code> · {row['plan']} · {row['stars']} ⭐"
        )
    total = (await db.stats())["stars"]
    lines.append(f"\nИтого: <b>{total}</b> ⭐")
    lines.append("\n<i>Вернуть деньги: /refund ID</i>")
    await show(call, "\n".join(lines), kb.admin_cancel())


@router.message(Command("refund"))
async def cmd_refund(message: Message, db: Database, cfg: Config, bot: Bot):
    """Возврат звёзд по последнему платежу пользователя."""
    if not _admin_only(cfg, message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: <code>/refund ID</code>")
        return
    user_id = int(parts[1])
    row = await db.last_payment_of(user_id)
    if row is None:
        await message.answer("У этого пользователя нет платежей к возврату.")
        return
    try:
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=row["charge_id"])
    except TelegramAPIError as exc:
        await message.answer(f"⚠️ Возврат не прошёл: <code>{exc}</code>")
        return
    await db.mark_refunded(row["charge_id"])
    await db.revoke_sub(user_id)
    await message.answer(f"↩️ Вернул <b>{row['stars']}</b> ⭐ пользователю <code>{user_id}</code>")


# ---------- настройки ----------


async def _settings_view(db: Database, cfg: Config) -> tuple[str, object]:
    trial_on, trial_days = await service.trial_settings(db, cfg)
    protect = bool(await db.get_int_setting("protect_content", int(cfg.protect_content)))
    autodelete = await db.get_int_setting("autodelete", cfg.autodelete)
    text = (
        f"⚙️ <b>Настройки</b>\n{t.SEP}\n"
        f"🎁 Тестовая подписка: <b>{'включена' if trial_on else 'выключена'}</b>\n"
        f"📅 Срок теста: <b>{trial_days} дн.</b>\n"
        f"🔒 Защита от пересылки: <b>{'вкл' if protect else 'выкл'}</b>\n"
        f"⏲ Автоудаление выданного: <b>{autodelete or 'выкл'}</b>"
        f"{' мин.' if autodelete else ''}\n\n"
        "<i>Меняется тапом по кнопке.</i>"
    )
    return text, kb.admin_settings(trial_on, trial_days, protect, autodelete)


@router.callback_query(kb.Adm.filter(F.act == "settings"))
async def adm_settings(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.clear()
    text, markup = await _settings_view(db, cfg)
    await show(call, text, markup)


@router.callback_query(kb.Adm.filter(F.act == "set_protect"))
async def adm_set_protect(call: CallbackQuery, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    current = bool(await db.get_int_setting("protect_content", int(cfg.protect_content)))
    await db.set_setting("protect_content", str(int(not current)))
    text, markup = await _settings_view(db, cfg)
    await show(call, text, markup)


@router.callback_query(kb.Adm.filter(F.act == "set_trial"))
async def adm_set_trial(call: CallbackQuery, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    current, _days = await service.trial_settings(db, cfg)
    await db.set_setting("trial_enabled", str(int(not current)))
    await call.answer("Тест включён" if not current else "Тест выключен")
    text, markup = await _settings_view(db, cfg)
    await show(call, text, markup)


@router.callback_query(kb.Adm.filter(F.act == "set_trial_days"))
async def adm_set_trial_days(call: CallbackQuery, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.set_trial_days)
    await show(call, "📅 На сколько дней давать тестовую подписку?", kb.admin_cancel())


@router.message(AdminFSM.set_trial_days)
async def adm_set_trial_days_do(
    message: Message, db: Database, cfg: Config, state: FSMContext
):
    value = parser.as_int(message.text or "", 0)
    if value <= 0:
        await message.answer("Нужно число больше нуля.")
        return
    await db.set_setting("trial_days", str(value))
    await state.clear()
    text, markup = await _settings_view(db, cfg)
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.Adm.filter(F.act == "set_autodelete"))
async def adm_set_autodelete(call: CallbackQuery, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.set_autodelete)
    await show(
        call,
        "⏲ Через сколько минут удалять выданное видео из чата? (0 — не удалять)",
        kb.admin_cancel(),
    )


@router.message(AdminFSM.set_autodelete)
async def adm_set_autodelete_do(message: Message, db: Database, cfg: Config, state: FSMContext):
    value = parser.as_int(message.text or "", -1)
    if value < 0:
        await message.answer("Нужно неотрицательное число.")
        return
    await db.set_setting("autodelete", str(value))
    await state.clear()
    text, markup = await _settings_view(db, cfg)
    await message.answer(text, reply_markup=markup)


@router.callback_query(kb.Adm.filter(F.act == "set_prices"))
async def adm_set_prices(call: CallbackQuery, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    plans = await service.plans(db)
    await show(
        call,
        f"💲 <b>Цены тарифов</b>\n{t.SEP}\nТап по тарифу — задать новую цену в звёздах.",
        kb.admin_prices(plans),
    )


@router.callback_query(kb.Adm.filter(F.act == "set_price"))
async def adm_set_price(call: CallbackQuery, callback_data: kb.Adm, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    codes = list(DEFAULT_PLANS.keys())
    if not 0 <= callback_data.arg < len(codes):
        return
    code = codes[callback_data.arg]
    await state.set_state(AdminFSM.set_price)
    await state.update_data(plan=code)
    await show(
        call,
        f"💲 Новая цена для тарифа <b>{DEFAULT_PLANS[code][0]}</b> — числом в звёздах:",
        kb.admin_cancel(),
    )


@router.message(AdminFSM.set_price)
async def adm_set_price_do(message: Message, db: Database, state: FSMContext):
    price = parser.as_int(message.text or "", 0)
    if price <= 0:
        await message.answer("Цена должна быть больше нуля.")
        return
    data = await state.get_data()
    await db.set_setting(f"price_{data['plan']}", str(price))
    await state.clear()
    plans = await service.plans(db)
    await message.answer(
        f"✅ Цена обновлена: <b>{price}</b> ⭐", reply_markup=kb.admin_prices(plans)
    )


# ---------- сервисные команды ----------


@router.message(Command("stats"))
async def cmd_stats(message: Message, db: Database, cfg: Config):
    if not _admin_only(cfg, message.from_user.id):
        return
    await message.answer(await _panel_text(db), reply_markup=kb.admin_panel())


@router.message(Command("pull"))
async def cmd_pull(
    message: Message, db: Database, cfg: Config, userbot: Userbot, bot: Bot
):
    """Залить файл с диска сервера юзерботом: /pull /путь | Название | сезон | серия | озвучка"""
    if not _admin_only(cfg, message.from_user.id):
        return
    raw = (message.text or "").split(maxsplit=1)
    if len(raw) < 2 or "|" not in raw[1]:
        await message.answer(
            "Формат:\n<code>/pull /путь/к/файлу | Название | 1 | 7 | Studio Band</code>"
        )
        return
    parts = [p.strip() for p in raw[1].split("|")]
    if len(parts) < 5:
        await message.answer("Нужно пять частей: путь, название, сезон, серия, озвучка.")
        return
    path, title, season, number, dub = parts[0], parts[1], parts[2], parts[3], parts[4]

    status = await message.answer("📤 Заливаю юзерботом, это небыстро…")
    caption = f"Название: {title}\nСезон: {season}\nСерия: {number}\nОзвучка: {dub}"
    message_id = await userbot.upload(cfg.storage_channel, path, caption)
    if message_id is None:
        await status.edit_text("⚠️ Не залилось. Проверь путь и сессию юзербота.")
        return

    anime_id = await db.add_anime(title, se.normalize(title))
    await db.add_episode(
        anime_id, parser.as_int(season, 1) or 1, parser.as_int(number), dub, message_id
    )
    await status.edit_text(f"✅ Залито: <b>{title}</b> S{season}E{number} · {dub}")


# ---------- предложения ----------


@router.callback_query(kb.Adm.filter(F.act == "sugg"))
async def adm_suggestions(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    rows = await db.top_suggestions(200)
    if not rows:
        await show(call, "💡 Предложений пока нет.", kb.admin_cancel())
        return
    per = 10
    pages = pages_of(len(rows), per)
    page = max(0, min(callback_data.p, pages - 1))
    chunk = rows[page * per : (page + 1) * per]
    text = (
        f"💡 <b>Предложения</b> ({len(rows)})\n{t.SEP}\n"
        "Отсортированы по голосам. Тап — карточка."
    )
    await show(call, text, kb.admin_suggestions(chunk, page, pages))


@router.callback_query(kb.Adm.filter(F.act == "sugg_one"))
async def adm_suggestion(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    row = await db.get_suggestion(callback_data.arg)
    if row is None:
        await show(call, "🤷 Предложение не найдено.", kb.admin_cancel())
        return
    votes = await db.votes_of(row["id"])
    aliases = await db.aliases_of(row["id"])
    text = (
        f"💡 <b>{row['title']}</b>\n{t.SEP}\n"
        f"🆔 <code>{row['id']}</code>\n"
        f"👍 Голосов: <b>{votes}</b>\n"
        f"🔤 Как писали: {', '.join(aliases) or '—'}"
    )
    await show(call, text, kb.admin_suggestion(row["id"]))


@router.callback_query(kb.Adm.filter(F.act == "sugg_done"))
async def adm_sugg_done(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    await db.set_suggestion_status(callback_data.arg, "done")
    await call.answer("Закрыл как залитое")
    await adm_suggestions(call, kb.Adm(act="sugg"), db, cfg)


@router.callback_query(kb.Adm.filter(F.act == "sugg_no"))
async def adm_sugg_reject(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    await db.set_suggestion_status(callback_data.arg, "rejected")
    await call.answer("Отклонил")
    await adm_suggestions(call, kb.Adm(act="sugg"), db, cfg)


@router.callback_query(kb.Adm.filter(F.act == "sugg_merge"))
async def adm_sugg_merge(call: CallbackQuery, callback_data: kb.Adm, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.set_state(AdminFSM.sugg_merge)
    await state.update_data(merge_src=callback_data.arg)
    await show(
        call,
        "🔗 Пришли <b>id того предложения</b>, в которое слить это.\n\n"
        "Голоса и написания переедут туда, это исчезнет.",
        kb.admin_cancel(),
    )


@router.message(AdminFSM.sugg_merge)
async def adm_sugg_merge_do(message: Message, db: Database, state: FSMContext):
    dst = parser.as_int(message.text or "", 0)
    data = await state.get_data()
    src = data.get("merge_src", 0)
    if not dst or dst == src:
        await message.answer("Нужен id другого предложения.")
        return
    if await db.get_suggestion(dst) is None:
        await message.answer("Предложения с таким id нет.")
        return
    await db.merge_suggestions(src, dst)
    await state.clear()
    votes = await db.votes_of(dst)
    row = await db.get_suggestion(dst)
    await message.answer(
        f"✅ Слил. Теперь у <b>{row['title']}</b> голосов: <b>{votes}</b>",
        reply_markup=kb.admin_panel(),
    )


# ---------- промокоды ----------


@router.callback_query(kb.Adm.filter(F.act == "promos"))
async def adm_promos(call: CallbackQuery, db: Database, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.clear()
    rows = await db.list_promos()
    text = (
        f"🎟 <b>Промокоды</b> ({len(rows)})\n{t.SEP}\n"
        "Формат строки: код · что даёт · использований."
        if rows
        else f"🎟 <b>Промокоды</b>\n{t.SEP}\nПока ни одного."
    )
    await show(call, text, kb.admin_promos(rows))


@router.callback_query(kb.Adm.filter(F.act == "promo_new"))
async def adm_promo_new(call: CallbackQuery, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await state.clear()
    await show(call, "🎟 <b>Новый промокод</b>\n\nЧто он будет давать?", kb.promo_kind())


@router.callback_query(kb.Adm.filter(F.act.in_({"promo_kind_sub", "promo_kind_disc"})))
async def adm_promo_kind(call: CallbackQuery, callback_data: kb.Adm, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    kind = "sub" if callback_data.act == "promo_kind_sub" else "discount"
    await state.set_state(AdminFSM.promo_value)
    await state.update_data(promo_kind=kind)
    question = (
        "⭐ На сколько <b>дней</b> подписки?"
        if kind == "sub"
        else "💲 Сколько <b>процентов</b> скидки? (1–99)"
    )
    await show(call, question, kb.admin_cancel())


@router.message(AdminFSM.promo_value)
async def adm_promo_value(message: Message, state: FSMContext):
    value = parser.as_int(message.text or "", 0)
    data = await state.get_data()
    kind = data.get("promo_kind", "sub")
    if value <= 0 or (kind == "discount" and value > 99):
        await message.answer("Нужно число" + (" от 1 до 99." if kind == "discount" else " больше нуля."))
        return
    await state.update_data(promo_amount=value)
    await state.set_state(AdminFSM.promo_code)
    await message.answer(
        "🎟 Теперь сам код. Пришли его текстом "
        "или напиши <code>-</code>, и я придумаю сам."
    )


@router.message(AdminFSM.promo_code)
async def adm_promo_code(message: Message, db: Database, state: FSMContext):
    raw = (message.text or "").strip().upper()
    if raw in {"-", ""}:
        raw = "AN" + secrets.token_hex(3).upper()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", raw):
        await message.answer("Код — 3–32 знака: латиница, цифры, дефис или подчёркивание.")
        return
    if await db.get_promo(raw) is not None:
        await message.answer("Такой код уже есть, придумай другой.")
        return
    await state.update_data(promo_code=raw)
    await state.set_state(AdminFSM.promo_uses)
    await message.answer(
        "🔢 Сколько раз можно использовать?\n"
        "Числом, или <code>-</code> — без ограничения."
    )


@router.message(AdminFSM.promo_uses)
async def adm_promo_uses(message: Message, db: Database, state: FSMContext):
    raw = (message.text or "").strip()
    max_uses = 0 if raw in {"-", ""} else parser.as_int(raw, 0)
    data = await state.get_data()
    kind = data.get("promo_kind", "sub")
    amount = data.get("promo_amount", 0)
    code = data.get("promo_code", "")

    await db.add_promo(
        code=code,
        kind=kind,
        days=amount if kind == "sub" else 0,
        percent=amount if kind == "discount" else 0,
        max_uses=max_uses,
    )
    await state.clear()

    what = f"подписку на {amount} дн." if kind == "sub" else f"скидку {amount}%"
    limit = "без ограничений" if not max_uses else f"{max_uses} раз"
    await message.answer(
        f"✅ <b>Промокод создан</b>\n{t.SEP}\n"
        f"Код: <code>{code}</code>\n"
        f"Даёт: {what}\n"
        f"Использований: {limit}",
        reply_markup=kb.admin_panel(),
    )


@router.callback_query(kb.Adm.filter(F.act == "promo_one"))
async def adm_promo_one(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config):
    if not _admin_only(cfg, call.from_user.id):
        return
    rows = [r for r in await db.list_promos(500) if r["id"] == callback_data.arg]
    if not rows:
        await show(call, "🤷 Промокод не найден.", kb.admin_cancel())
        return
    row = rows[0]
    what = f"подписка {row['days']} дн." if row["kind"] == "sub" else f"скидка {row['percent']}%"
    limit = "без ограничений" if not row["max_uses"] else f"{row['used']} из {row['max_uses']}"
    await show(
        call,
        f"🎟 <b>{row['code']}</b>\n{t.SEP}\n"
        f"Даёт: {what}\n"
        f"Использован: {limit}",
        kb.admin_promo_one(row["id"]),
    )


@router.callback_query(kb.Adm.filter(F.act == "promo_del"))
async def adm_promo_del(call: CallbackQuery, callback_data: kb.Adm, db: Database, cfg: Config, state: FSMContext):
    if not _admin_only(cfg, call.from_user.id):
        return
    await db.delete_promo(callback_data.arg)
    await call.answer("Удалён")
    await adm_promos(call, db, cfg, state)
