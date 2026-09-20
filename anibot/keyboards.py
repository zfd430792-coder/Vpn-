"""Инлайн-клавиатуры и фабрики callback-данных."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .db import Anime, Episode

PER_PAGE = 8
EPISODES_PER_PAGE = 40
EPISODE_COLUMNS = 5


class Nav(CallbackData, prefix="n"):
    """Навигация по каталогу. i=anime_id или episode_id, s=сезон, e=серия, p=страница."""

    to: str
    i: int = 0
    s: int = 0
    e: int = 0
    p: int = 0


class Pay(CallbackData, prefix="p"):
    plan: str


class Adm(CallbackData, prefix="a"):
    act: str
    arg: int = 0
    p: int = 0


def _nav(text: str, to: str, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Nav(to=to, **kw).pack())


def _pager(to: str, page: int, pages: int, **kw) -> list[InlineKeyboardButton]:
    """Ряд листалки. Пустой, если страница одна."""
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row.append(_nav("◀️", to, p=page - 1, **kw))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(_nav("▶️", to, p=page + 1, **kw))
    return row


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_nav("🔍 Поиск", "search"), _nav("📚 Каталог", "catalog"))
    kb.row(_nav("🔥 Популярное", "popular"), _nav("💡 Предложить", "suggest"))
    kb.row(_nav("⭐ Подписка", "subs"), _nav("👤 Профиль", "profile"))
    kb.row(_nav("💬 Поддержка", "support"), _nav("❓ Помощь", "help"))
    if is_admin:
        kb.row(_nav("⚙️ Админка", "admin"))
    return kb.as_markup()


def back_to(to: str = "menu", **kw) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_nav("🏠 В меню", to, **kw))
    return kb.as_markup()


def anime_list(
    items: list[Anime], page: int, pages: int, to: str, title_prefix: str = "🎬"
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for anime in items:
        kb.row(_nav(f"{title_prefix} {anime.title}"[:60], "anime", i=anime.id))
    pager = _pager(to, page, pages)
    if pager:
        kb.row(*pager)
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def seasons(anime_id: int, season_list: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for season in season_list:
        row.append(_nav(f"📀 Сезон {season}", "season", i=anime_id, s=season))
        if len(row) == 2:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    kb.row(_nav("⬅️ Назад", "catalog"), _nav("🏠 В меню", "menu"))
    return kb.as_markup()


def episodes(
    anime_id: int, season: int, numbers: list[int], page: int
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    pages = max(1, (len(numbers) + EPISODES_PER_PAGE - 1) // EPISODES_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = numbers[page * EPISODES_PER_PAGE : (page + 1) * EPISODES_PER_PAGE]

    row: list[InlineKeyboardButton] = []
    for number in chunk:
        row.append(_nav(str(number), "ep", i=anime_id, s=season, e=number))
        if len(row) == EPISODE_COLUMNS:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)

    pager = _pager("season", page, pages, i=anime_id, s=season)
    if pager:
        kb.row(*pager)
    kb.row(_nav("⬅️ К сезонам", "anime", i=anime_id), _nav("🏠 В меню", "menu"))
    return kb.as_markup()


def dubs(anime_id: int, season: int, number: int, variants: list[Episode]) -> InlineKeyboardMarkup:
    """По одной строке на озвучку. В variants — представитель каждой озвучки."""
    kb = InlineKeyboardBuilder()
    for ep in variants:
        kb.row(_nav(f"🎙 {ep.dub}"[:60], "dub", i=ep.id))
    kb.row(
        _nav("⬅️ К сериям", "season", i=anime_id, s=season),
        _nav("🏠 В меню", "menu"),
    )
    return kb.as_markup()


def qualities(variants: list[Episode], anime_id: int, season: int, number: int) -> InlineKeyboardMarkup:
    """Выбор качества внутри выбранной озвучки. Лучшее — сверху."""
    kb = InlineKeyboardBuilder()
    for ep in variants:
        mark = "💎" if ep.quality >= 2160 else "🎬"
        size = f" · {ep.file_size / 1024 ** 3:.1f} ГБ" if ep.file_size else ""
        kb.row(_nav(f"{mark} {ep.quality_name}{size}", "watch", i=ep.id))
    kb.row(
        _nav("⬅️ К озвучкам", "ep", i=anime_id, s=season, e=number),
        _nav("🏠 В меню", "menu"),
    )
    return kb.as_markup()


def player(
    ep: Episode,
    has_prev: bool,
    has_next: bool,
    dub_count: int,
    quality_count: int = 1,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    if has_prev:
        row.append(_nav("⏮ Пред.", "step", i=ep.id, e=-1))
    if has_next:
        row.append(_nav("След. ⏭", "step", i=ep.id, e=1))
    if row:
        kb.row(*row)
    bottom = []
    if dub_count > 1:
        bottom.append(_nav("🎚 Озвучка", "ep", i=ep.anime_id, s=ep.season, e=ep.number))
    if quality_count > 1:
        bottom.append(_nav("💎 Качество", "dub", i=ep.id))
    if bottom:
        kb.row(*bottom)
    kb.row(_nav("📋 Все серии", "season", i=ep.anime_id, s=ep.season))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def subscription(
    plans: dict[str, tuple[str, int, int]],
    has_sub: bool,
    trial_available: bool = False,
    trial_days: int = 0,
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if trial_available:
        kb.row(_nav(f"🎁 Тестовая подписка на {trial_days} дн.", "trial"))
    for code, (label, days, stars) in plans.items():
        suffix = "продлить" if has_sub else f"{days} дн."
        if days > 3650:
            suffix = "навсегда"
        kb.row(
            InlineKeyboardButton(
                text=f"⭐ {label} — {stars} звёзд ({suffix})",
                callback_data=Pay(plan=code).pack(),
            )
        )
    kb.row(_nav("🎟 Промокод", "promo"))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def paywall(trial_available: bool = False, trial_days: int = 0) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if trial_available:
        kb.row(_nav(f"🎁 Включить тест на {trial_days} дн.", "trial"))
    kb.row(_nav("⭐ Оформить подписку", "subs"))
    kb.row(_nav("🎟 У меня промокод", "promo"))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def suggestions(items: list, voted: set[int] | None = None) -> InlineKeyboardMarkup:
    """Список предложений с голосами. Тап — голос."""
    voted = voted or set()
    kb = InlineKeyboardBuilder()
    for row in items:
        mark = "✅" if row["id"] in voted else "👍"
        kb.row(_nav(f"{mark} {row['votes']} · {row['title']}"[:60], "sgvote", i=row["id"]))
    kb.row(_nav("➕ Предложить своё", "suggest"))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def suggest_which(matches: list, token: int) -> InlineKeyboardMarkup:
    """Спрашиваем, что человек имел в виду: спорные варианты + «новое»."""
    kb = InlineKeyboardBuilder()
    for m in matches:
        kb.row(
            _nav(
                f"👍 {m.candidate.title}"[:60],
                "sgpick",
                i=m.candidate.id,
                p=token,
            )
        )
    kb.row(_nav("➕ Нет, это другое — завести новое", "sgnew", p=token))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


# ---------- админка ----------


def _adm(text: str, act: str, **kw) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=Adm(act=act, **kw).pack())


def admin_panel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("📥 Как добавлять", "howto"), _adm("🎬 Тайтлы", "titles"))
    kb.row(_adm("📣 Рассылка", "broadcast"), _adm("⭐ Выдать подписку", "grant"))
    kb.row(_adm("🚫 Бан / разбан", "ban"), _adm("💰 Платежи", "payments"))
    kb.row(_adm("💡 Предложения", "sugg"), _adm("🎟 Промокоды", "promos"))
    kb.row(_adm("🔎 Проверить тайтлы", "scan"))
    kb.row(_adm("⚙️ Настройки", "settings"), _adm("🔄 Обновить", "refresh"))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def admin_titles(items: list[Anime], page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for anime in items:
        kb.row(_adm(f"🎬 {anime.title}"[:60], "title", arg=anime.id))
    pager = []
    if pages > 1:
        if page > 0:
            pager.append(_adm("◀️", "titles", p=page - 1))
        pager.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            pager.append(_adm("▶️", "titles", p=page + 1))
    if pager:
        kb.row(*pager)
    kb.row(_adm("⬅️ В админку", "refresh"))
    return kb.as_markup()


def admin_title(anime_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("✏️ Переименовать", "rename", arg=anime_id))
    kb.row(_adm("🗑 Удалить тайтл", "del_ask", arg=anime_id))
    kb.row(_nav("👀 Посмотреть как юзер", "anime", i=anime_id))
    kb.row(_adm("⬅️ К тайтлам", "titles"))
    return kb.as_markup()


def admin_confirm_delete(anime_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("🗑 Да, удалить", "del_yes", arg=anime_id))
    kb.row(_adm("⬅️ Отмена", "title", arg=anime_id))
    return kb.as_markup()


def admin_settings(
    trial_on: bool, trial_days: int, protect: bool, autodelete: int
) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm(f"🎁 Тестовая подписка: {'вкл' if trial_on else 'выкл'}", "set_trial"))
    kb.row(_adm(f"📅 Срок теста: {trial_days} дн.", "set_trial_days"))
    kb.row(_adm(f"🔒 Защита от пересылки: {'вкл' if protect else 'выкл'}", "set_protect"))
    kb.row(_adm(f"⏲ Автоудаление: {autodelete or 'выкл'}", "set_autodelete"))
    kb.row(_adm("💲 Цены тарифов", "set_prices"))
    kb.row(_adm("⬅️ В админку", "refresh"))
    return kb.as_markup()


def admin_prices(plans: dict[str, tuple[str, int, int]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for idx, (code, (label, _days, stars)) in enumerate(plans.items()):
        kb.row(_adm(f"⭐ {label}: {stars}", "set_price", arg=idx))
    kb.row(_adm("⬅️ К настройкам", "settings"))
    return kb.as_markup()


def admin_cancel() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("✖️ Отмена", "refresh"))
    return kb.as_markup()


def upload_confirm() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("✅ Сохранить", "up_save"), _adm("✖️ Отмена", "up_cancel"))
    return kb.as_markup()


def admin_suggestions(items: list, page: int, pages: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row in items:
        kb.row(_adm(f"{row['votes']} · {row['title']}"[:60], "sugg_one", arg=row["id"]))
    pager = []
    if pages > 1:
        if page > 0:
            pager.append(_adm("◀️", "sugg", p=page - 1))
        pager.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            pager.append(_adm("▶️", "sugg", p=page + 1))
    if pager:
        kb.row(*pager)
    kb.row(_adm("⬅️ В админку", "refresh"))
    return kb.as_markup()


def admin_suggestion(suggestion_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("✅ Залито — закрыть", "sugg_done", arg=suggestion_id))
    kb.row(_adm("🚫 Отклонить", "sugg_no", arg=suggestion_id))
    kb.row(_adm("🔗 Слить с другим", "sugg_merge", arg=suggestion_id))
    kb.row(_adm("⬅️ К предложениям", "sugg"))
    return kb.as_markup()


def admin_promos(items: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for row in items:
        if row["kind"] == "sub":
            what = f"{row['days']} дн."
        else:
            what = f"-{row['percent']}%"
        limit = "∞" if not row["max_uses"] else f"{row['used']}/{row['max_uses']}"
        kb.row(_adm(f"🎟 {row['code']} · {what} · {limit}", "promo_one", arg=row["id"]))
    kb.row(_adm("➕ Создать промокод", "promo_new"))
    kb.row(_adm("⬅️ В админку", "refresh"))
    return kb.as_markup()


def admin_promo_one(promo_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("🗑 Удалить", "promo_del", arg=promo_id))
    kb.row(_adm("⬅️ К промокодам", "promos"))
    return kb.as_markup()


def promo_kind() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.row(_adm("⭐ Даёт подписку", "promo_kind_sub"))
    kb.row(_adm("💲 Даёт скидку", "promo_kind_disc"))
    kb.row(_adm("✖️ Отмена", "refresh"))
    return kb.as_markup()


def watch_hit(anime_id: int) -> InlineKeyboardMarkup:
    """Кнопка из уведомления «появилось» — сразу в карточку тайтла."""
    kb = InlineKeyboardBuilder()
    kb.row(_nav("▶️ Смотреть", "anime", i=anime_id))
    kb.row(_nav("🏠 В меню", "menu"))
    return kb.as_markup()


def not_found(query_known: bool = False) -> InlineKeyboardMarkup:
    """Экран «не нашлось»: подписаться на появление."""
    kb = InlineKeyboardBuilder()
    if query_known:
        kb.row(InlineKeyboardButton(text="🔔 Уже в списке ожидания", callback_data="noop"))
    else:
        kb.row(_nav("🔔 Сообщить, когда появится", "wantit"))
    kb.row(_nav("📚 Каталог", "catalog"), _nav("🏠 В меню", "menu"))
    return kb.as_markup()
