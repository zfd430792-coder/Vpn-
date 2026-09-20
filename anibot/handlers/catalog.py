"""Каталог, популярное, поиск и навигация: тайтл → сезон → серия → озвучка."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from .. import search as se
from .. import texts as t
from ..db import Database
from .common import pages_of, show

router = Router(name="catalog")

RESULTS_KEY = "results"


async def _anime_card(db: Database, anime_id: int) -> tuple[str, list[int]] | None:
    anime = await db.get_anime(anime_id)
    if anime is None:
        return None
    seasons, episodes, dubs, quals = await db.anime_summary(anime_id)
    text = t.ANIME_CARD.format(
        title=anime.title,
        seasons=seasons or 1,
        episodes=episodes,
        dubs=", ".join(dubs) if dubs else "—",
        quality=", ".join({1080: "1080p", 2160: "4K"}.get(q, f"{q}p") for q in quals) or "—",
    )
    return text, await db.seasons(anime_id)


# ---------- каталог ----------


@router.callback_query(kb.Nav.filter(F.to == "catalog"))
async def nav_catalog(call: CallbackQuery, callback_data: kb.Nav, db: Database):
    total = await db.count_anime()
    if not total:
        await show(call, t.EMPTY_CATALOG, kb.back_to())
        return
    pages = pages_of(total, kb.PER_PAGE)
    page = max(0, min(callback_data.p, pages - 1))
    items = await db.list_anime(page * kb.PER_PAGE, kb.PER_PAGE)
    text = f"📚 <b>Каталог</b>\n{t.SEP}\nВсего тайтлов: <b>{total}</b>"
    await show(call, text, kb.anime_list(items, page, pages, "catalog"))


@router.callback_query(kb.Nav.filter(F.to == "popular"))
async def nav_popular(call: CallbackQuery, db: Database):
    items = await db.popular(10)
    if not items:
        await show(call, t.EMPTY_CATALOG, kb.back_to())
        return
    text = f"🔥 <b>Популярное</b>\n{t.SEP}\nЧто чаще всего смотрят:"
    await show(call, text, kb.anime_list(items, 0, 1, "popular", "🔥"))


# ---------- поиск ----------


@router.callback_query(kb.Nav.filter(F.to == "sr"))
async def nav_search_page(
    call: CallbackQuery, callback_data: kb.Nav, db: Database, state: FSMContext
):
    data = await state.get_data()
    ids: list[int] = data.get(RESULTS_KEY, [])
    if not ids:
        await show(call, "🔍 Поиск устарел — набери запрос заново.", kb.back_to())
        return
    pages = pages_of(len(ids), kb.PER_PAGE)
    page = max(0, min(callback_data.p, pages - 1))
    chunk = ids[page * kb.PER_PAGE : (page + 1) * kb.PER_PAGE]
    items = [a for a in [await db.get_anime(i) for i in chunk] if a]
    text = f"🔍 <b>Найдено: {len(ids)}</b>\n{t.SEP}\nВыбери тайтл:"
    await show(call, text, kb.anime_list(items, page, pages, "sr"))


@router.message(
    StateFilter(None), F.chat.type == "private", F.text & ~F.text.startswith("/")
)
async def search_text(message: Message, db: Database, state: FSMContext):
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("🔍 Слишком короткий запрос — от двух символов.")
        return

    norm = se.normalize(query)
    found = await db.search_anime(norm)
    if not found:
        # подстраховка: нечёткое сравнение по всему каталогу
        everything = await db.all_anime()
        order = se.rank(query, [(a.id, a.norm) for a in everything])
        by_id = {a.id: a for a in everything}
        found = [by_id[i] for i in order if i in by_id][:30]

    if not found:
        await message.answer(t.NOT_FOUND.format(query=query), reply_markup=kb.back_to())
        return

    if len(found) == 1:
        card = await _anime_card(db, found[0].id)
        if card:
            text, seasons = card
            await message.answer(text, reply_markup=kb.seasons(found[0].id, seasons))
            return

    ids = [a.id for a in found]
    await state.update_data({RESULTS_KEY: ids})
    pages = pages_of(len(ids), kb.PER_PAGE)
    text = f"🔍 <b>Найдено: {len(ids)}</b>\n{t.SEP}\nВыбери тайтл:"
    await message.answer(
        text, reply_markup=kb.anime_list(found[: kb.PER_PAGE], 0, pages, "sr")
    )


# ---------- тайтл → сезон → серия → озвучка ----------


@router.callback_query(kb.Nav.filter(F.to == "anime"))
async def nav_anime(call: CallbackQuery, callback_data: kb.Nav, db: Database):
    card = await _anime_card(db, callback_data.i)
    if card is None:
        await show(call, "🤷 Тайтл не найден — возможно, его удалили.", kb.back_to())
        return
    text, seasons = card
    if not seasons:
        await show(call, text + "\n\n📭 Серий пока нет.", kb.back_to())
        return
    if len(seasons) == 1:
        await _show_episodes(call, db, callback_data.i, seasons[0], 0)
        return
    await show(call, text, kb.seasons(callback_data.i, seasons))


async def _show_episodes(
    call: CallbackQuery, db: Database, anime_id: int, season: int, page: int
) -> None:
    anime = await db.get_anime(anime_id)
    numbers = await db.episode_numbers(anime_id, season)
    if anime is None or not numbers:
        await show(call, "📭 В этом сезоне серий нет.", kb.back_to())
        return
    text = (
        f"🎬 <b>{anime.title}</b>\n{t.SEP}\n"
        f"📀 Сезон <b>{season}</b>   ·   🎞 Серий: <b>{len(numbers)}</b>\n\n"
        "Выбери серию:"
    )
    await show(call, text, kb.episodes(anime_id, season, numbers, page))


@router.callback_query(kb.Nav.filter(F.to == "season"))
async def nav_season(call: CallbackQuery, callback_data: kb.Nav, db: Database):
    await _show_episodes(call, db, callback_data.i, callback_data.s, callback_data.p)


@router.callback_query(kb.Nav.filter(F.to == "ep"))
async def nav_episode(call: CallbackQuery, callback_data: kb.Nav, db: Database, **kwargs):
    """Шаг выбора озвучки. Одна озвучка — сразу к качеству."""
    names = await db.dub_names(callback_data.i, callback_data.s, callback_data.e)
    if not names:
        await show(call, "📭 Этой серии нет.", kb.back_to())
        return

    # представитель каждой озвучки — по нему дальше берём список качеств
    reps = []
    for name in names:
        variants = await db.qualities(callback_data.i, callback_data.s, callback_data.e, name)
        if variants:
            reps.append(variants[0])

    if len(reps) == 1:
        await _show_qualities(call, db, reps[0], **kwargs)
        return

    anime = await db.get_anime(callback_data.i)
    title = anime.title if anime else "—"
    text = (
        f"🎬 <b>{title}</b>\n{t.SEP}\n"
        f"📀 Сезон {callback_data.s} · Серия {callback_data.e}\n\n"
        "🎙 Выбери озвучку:"
    )
    await show(call, text, kb.dubs(callback_data.i, callback_data.s, callback_data.e, reps))


async def _show_qualities(call: CallbackQuery, db: Database, ep, **kwargs) -> None:
    """Шаг выбора качества. Качество одно — отдаём серию без лишнего вопроса."""
    variants = await db.qualities(ep.anime_id, ep.season, ep.number, ep.dub)
    if not variants:
        await show(call, "📭 Этой серии нет.", kb.back_to())
        return

    if len(variants) == 1:
        from .watch import send_episode

        await send_episode(call, variants[0], db=db, **kwargs)
        return

    anime = await db.get_anime(ep.anime_id)
    title = anime.title if anime else "—"
    text = (
        f"🎬 <b>{title}</b>\n{t.SEP}\n"
        f"📀 Сезон {ep.season} · Серия {ep.number}\n"
        f"🎙 {ep.dub}\n\n"
        "💎 Выбери качество:"
    )
    await show(call, text, kb.qualities(variants, ep.anime_id, ep.season, ep.number))


@router.callback_query(kb.Nav.filter(F.to == "dub"))
async def nav_dub(call: CallbackQuery, callback_data: kb.Nav, db: Database, **kwargs):
    ep = await db.get_episode(callback_data.i)
    if ep is None:
        await show(call, "📭 Серия не найдена.", kb.back_to())
        return
    await _show_qualities(call, db, ep, **kwargs)
