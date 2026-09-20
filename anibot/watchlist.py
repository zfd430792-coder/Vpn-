"""Сверка заявок «сообщить, когда появится» с каталогом.

Работает в двух режимах:

* сама, при каждой заливке — новый тайтл сверяется с ожидающими заявками;
* по кнопке в админке — полный проход по всему каталогу, на случай если
  что-то залили мимо бота или он в этот момент лежал.

Сопоставление то же, что у предложений: человек мог написать сокращение
или с опечаткой, а в каталоге тайтл лежит под полным названием.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from . import keyboards as kb
from . import notify
from . import suggest
from .config import Config
from .db import Anime, Database

log = logging.getLogger("anibot.watchlist")


@dataclass
class Hit:
    watch_id: int
    user_id: int
    asked: str
    anime: Anime
    score: float


def _as_candidate(anime: Anime) -> suggest.Candidate:
    aliases = [a for a in (anime.aliases or "").split("|") if a]
    return suggest.Candidate(anime.id, anime.title, anime.norm, 0, aliases)


def match_watch(asked: str, animes: list[Anime]) -> tuple[Anime | None, float]:
    """Находит тайтл в каталоге по тому, как человек его записал."""
    best: tuple[Anime | None, float] = (None, 0.0)
    for anime in animes:
        value = suggest.score(asked, _as_candidate(anime))
        if value > best[1]:
            best = (anime, value)
    if best[1] >= suggest.SURE:
        return best
    return None, best[1]


async def collect(db: Database, animes: list[Anime]) -> list[Hit]:
    """Какие заявки закрываются этими тайтлами."""
    hits: list[Hit] = []
    for row in await db.open_watches():
        anime, value = match_watch(row["title"], animes)
        if anime is None:
            continue
        hits.append(Hit(row["id"], row["user_id"], row["title"], anime, value))
    return hits


async def announce(bot: Bot, db: Database, hits: list[Hit]) -> int:
    """Рассылает уведомления и закрывает заявки. Возвращает, сколько дошло."""
    sent = 0
    for hit in hits:
        text = (
            "🔔 <b>Появилось то, что ты просил</b>\n"
            f"{'━' * 15}\n"
            f"🎬 <b>{hit.anime.title}</b>\n\n"
            f"<i>Ты писал: {hit.asked}</i>"
        )
        try:
            await bot.send_message(
                hit.user_id,
                text,
                reply_markup=kb.watch_hit(hit.anime.id),
            )
            sent += 1
        except TelegramAPIError as exc:
            # человек мог заблокировать бота — заявку всё равно закрываем
            log.info("Уведомление %s не дошло: %s", hit.user_id, exc)
        await db.mark_notified(hit.watch_id)
    return sent


async def close_suggestions(db: Database, animes: list[Anime]) -> list[str]:
    """Закрывает предложения, которые уже есть в каталоге."""
    closed: list[str] = []
    for row in await db.top_suggestions(500):
        anime, _value = match_watch(row["title"], animes)
        if anime is not None:
            await db.set_suggestion_status(row["id"], "done")
            closed.append(f"{row['title']} → {anime.title}")
    return closed


async def on_new_title(bot: Bot, cfg: Config, db: Database, anime: Anime) -> int:
    """Вызывается после заливки. Сверяет заявки с одним новым тайтлом."""
    hits = await collect(db, [anime])
    if not hits:
        return 0
    sent = await announce(bot, db, hits)
    with contextlib.suppress(Exception):
        await notify.send(
            bot,
            cfg,
            notify.SUGGESTIONS,
            f"🔔 <b>{anime.title}</b> закрыл заявок: <b>{len(hits)}</b>, "
            f"уведомлено: <b>{sent}</b>",
        )
    return sent


async def full_scan(bot: Bot, db: Database) -> tuple[int, int, list[str]]:
    """Полный проход по каталогу. (найдено заявок, уведомлено, закрытые предложения)"""
    animes = await db.all_anime()
    if not animes:
        return 0, 0, []
    hits = await collect(db, animes)
    sent = await announce(bot, db, hits)
    closed = await close_suggestions(db, animes)
    return len(hits), sent, closed
