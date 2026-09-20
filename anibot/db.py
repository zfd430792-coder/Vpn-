"""SQLite-слой: каталог, пользователи, подписки, платежи, настройки."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS anime (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    norm       TEXT    NOT NULL,
    aliases    TEXT    NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_anime_norm ON anime(norm);

CREATE TABLE IF NOT EXISTS episode (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_id   INTEGER NOT NULL REFERENCES anime(id) ON DELETE CASCADE,
    season     INTEGER NOT NULL DEFAULT 1,
    number     INTEGER NOT NULL,
    dub        TEXT    NOT NULL,
    message_id INTEGER NOT NULL,
    file_size  INTEGER NOT NULL DEFAULT 0,
    duration   INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE(anime_id, season, number, dub)
);
CREATE INDEX IF NOT EXISTS idx_ep_anime ON episode(anime_id, season, number);

CREATE TABLE IF NOT EXISTS tg_user (
    id        INTEGER PRIMARY KEY,
    username  TEXT,
    name      TEXT,
    joined_at INTEGER NOT NULL,
    seen_at   INTEGER NOT NULL,
    banned    INTEGER NOT NULL DEFAULT 0,
    sub_until INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS view (
    user_id    INTEGER NOT NULL,
    episode_id INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (user_id, episode_id)
);

CREATE TABLE IF NOT EXISTS payment (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    plan      TEXT    NOT NULL,
    stars     INTEGER NOT NULL,
    charge_id TEXT    NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL,
    refunded  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promo (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL UNIQUE,
    kind       TEXT    NOT NULL,              -- 'sub' | 'discount'
    days       INTEGER NOT NULL DEFAULT 0,    -- для kind='sub'
    percent    INTEGER NOT NULL DEFAULT 0,    -- для kind='discount'
    max_uses   INTEGER NOT NULL DEFAULT 0,    -- 0 = без ограничения
    used       INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL DEFAULT 0,    -- 0 = бессрочно
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS promo_use (
    promo_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    ts       INTEGER NOT NULL,
    PRIMARY KEY (promo_id, user_id)
);

CREATE TABLE IF NOT EXISTS suggestion (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    norm       TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'new',   -- new | done | rejected
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sugg_status ON suggestion(status);

CREATE TABLE IF NOT EXISTS suggestion_alias (
    suggestion_id INTEGER NOT NULL,
    alias         TEXT    NOT NULL,
    PRIMARY KEY (suggestion_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias ON suggestion_alias(alias);

CREATE TABLE IF NOT EXISTS suggestion_vote (
    suggestion_id INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    PRIMARY KEY (suggestion_id, user_id)
);
"""

# Колонки, которые появились после первой версии. SQLite не умеет
# "ADD COLUMN IF NOT EXISTS", поэтому смотрим, чего не хватает, и дополняем.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "tg_user": [
        ("trial_used", "INTEGER NOT NULL DEFAULT 0"),
        ("discount", "INTEGER NOT NULL DEFAULT 0"),
        ("discount_promo", "TEXT NOT NULL DEFAULT ''"),
    ],
}


@dataclass
class Anime:
    id: int
    title: str
    norm: str
    aliases: str = ""


@dataclass
class Episode:
    id: int
    anime_id: int
    season: int
    number: int
    dub: str
    message_id: int
    file_size: int = 0
    duration: int = 0


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._db: Optional[aiosqlite.Connection] = None

    # ---------- жизненный цикл ----------

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        await self._migrate()

    async def _migrate(self) -> None:
        """Дописывает колонки, которых нет в уже существующей базе."""
        for table, columns in MIGRATIONS.items():
            async with self.db.execute(f"PRAGMA table_info({table})") as cur:
                have = {row["name"] for row in await cur.fetchall()}
            for name, spec in columns:
                if name not in have:
                    await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
        await self.db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() не вызван")
        return self._db

    async def _fetchone(self, sql: str, args: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
        async with self.db.execute(sql, tuple(args)) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, args: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, tuple(args)) as cur:
            return list(await cur.fetchall())

    async def _exec(self, sql: str, args: Iterable[Any] = ()) -> int:
        cur = await self.db.execute(sql, tuple(args))
        await self.db.commit()
        return cur.lastrowid or 0

    # ---------- настройки ----------

    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._fetchone("SELECT value FROM setting WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._exec(
            "INSERT INTO setting(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    async def get_int_setting(self, key: str, default: int) -> int:
        raw = await self.get_setting(key)
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    # ---------- пользователи ----------

    async def touch_user(self, user_id: int, username: str | None, name: str | None) -> None:
        ts = now()
        await self._exec(
            "INSERT INTO tg_user(id, username, name, joined_at, seen_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, "
            "name=excluded.name, seen_at=excluded.seen_at",
            (user_id, username, name, ts, ts),
        )

    async def get_user(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone("SELECT * FROM tg_user WHERE id = ?", (user_id,))

    async def set_banned(self, user_id: int, banned: bool) -> None:
        await self._exec("UPDATE tg_user SET banned = ? WHERE id = ?", (int(banned), user_id))

    async def is_banned(self, user_id: int) -> bool:
        row = await self._fetchone("SELECT banned FROM tg_user WHERE id = ?", (user_id,))
        return bool(row and row["banned"])

    async def all_user_ids(self) -> list[int]:
        rows = await self._fetchall("SELECT id FROM tg_user WHERE banned = 0")
        return [r["id"] for r in rows]

    # ---------- подписка ----------

    async def sub_until(self, user_id: int) -> int:
        row = await self._fetchone("SELECT sub_until FROM tg_user WHERE id = ?", (user_id,))
        return int(row["sub_until"]) if row else 0

    async def has_sub(self, user_id: int) -> bool:
        return await self.sub_until(user_id) > now()

    async def grant_sub(self, user_id: int, days: int) -> int:
        """Продлевает подписку от текущего конца (или от сейчас). Возвращает новый конец."""
        current = max(await self.sub_until(user_id), now())
        until = current + days * 86400
        await self._exec("UPDATE tg_user SET sub_until = ? WHERE id = ?", (until, user_id))
        return until

    async def revoke_sub(self, user_id: int) -> None:
        await self._exec("UPDATE tg_user SET sub_until = 0 WHERE id = ?", (user_id,))

    # ---------- просмотры (бесплатная квота) ----------

    async def add_view(self, user_id: int, episode_id: int) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO view(user_id, episode_id, ts) VALUES(?,?,?)",
            (user_id, episode_id, now()),
        )

    async def views_count(self, user_id: int) -> int:
        row = await self._fetchone("SELECT COUNT(*) c FROM view WHERE user_id = ?", (user_id,))
        return int(row["c"]) if row else 0

    async def has_seen(self, user_id: int, episode_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM view WHERE user_id = ? AND episode_id = ?", (user_id, episode_id)
        )
        return row is not None

    # ---------- платежи ----------

    async def add_payment(self, user_id: int, plan: str, stars: int, charge_id: str) -> int:
        return await self._exec(
            "INSERT INTO payment(user_id, plan, stars, charge_id, ts) VALUES(?,?,?,?,?)",
            (user_id, plan, stars, charge_id, now()),
        )

    async def mark_refunded(self, charge_id: str) -> None:
        await self._exec("UPDATE payment SET refunded = 1 WHERE charge_id = ?", (charge_id,))

    async def last_payments(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT * FROM payment ORDER BY id DESC LIMIT ?", (limit,)
        )

    async def last_payment_of(self, user_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone(
            "SELECT * FROM payment WHERE user_id = ? AND refunded = 0 ORDER BY id DESC LIMIT 1",
            (user_id,),
        )

    # ---------- каталог: аниме ----------

    async def add_anime(self, title: str, norm: str) -> int:
        row = await self._fetchone("SELECT id FROM anime WHERE norm = ?", (norm,))
        if row:
            return int(row["id"])
        return await self._exec(
            "INSERT INTO anime(title, norm, created_at) VALUES(?,?,?)", (title, norm, now())
        )

    async def get_anime(self, anime_id: int) -> Optional[Anime]:
        row = await self._fetchone("SELECT * FROM anime WHERE id = ?", (anime_id,))
        if not row:
            return None
        return Anime(row["id"], row["title"], row["norm"], row["aliases"])

    async def rename_anime(self, anime_id: int, title: str, norm: str) -> None:
        await self._exec(
            "UPDATE anime SET title = ?, norm = ? WHERE id = ?", (title, norm, anime_id)
        )

    async def delete_anime(self, anime_id: int) -> None:
        await self._exec("DELETE FROM episode WHERE anime_id = ?", (anime_id,))
        await self._exec("DELETE FROM anime WHERE id = ?", (anime_id,))

    async def list_anime(self, offset: int = 0, limit: int = 8) -> list[Anime]:
        rows = await self._fetchall(
            "SELECT * FROM anime ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?", (limit, offset)
        )
        return [Anime(r["id"], r["title"], r["norm"], r["aliases"]) for r in rows]

    async def count_anime(self) -> int:
        row = await self._fetchone("SELECT COUNT(*) c FROM anime")
        return int(row["c"]) if row else 0

    async def all_anime(self) -> list[Anime]:
        rows = await self._fetchall("SELECT * FROM anime")
        return [Anime(r["id"], r["title"], r["norm"], r["aliases"]) for r in rows]

    async def search_anime(self, norm_query: str, limit: int = 30) -> list[Anime]:
        rows = await self._fetchall(
            "SELECT * FROM anime WHERE norm LIKE ? OR aliases LIKE ? "
            "ORDER BY LENGTH(title) LIMIT ?",
            (f"%{norm_query}%", f"%{norm_query}%", limit),
        )
        return [Anime(r["id"], r["title"], r["norm"], r["aliases"]) for r in rows]

    async def popular(self, limit: int = 10) -> list[Anime]:
        rows = await self._fetchall(
            "SELECT a.*, COUNT(v.episode_id) c FROM anime a "
            "JOIN episode e ON e.anime_id = a.id "
            "LEFT JOIN view v ON v.episode_id = e.id "
            "GROUP BY a.id ORDER BY c DESC, a.title LIMIT ?",
            (limit,),
        )
        return [Anime(r["id"], r["title"], r["norm"], r["aliases"]) for r in rows]

    # ---------- каталог: серии ----------

    async def add_episode(
        self,
        anime_id: int,
        season: int,
        number: int,
        dub: str,
        message_id: int,
        file_size: int = 0,
        duration: int = 0,
    ) -> int:
        """Вставляет или обновляет серию. Возвращает id строки."""
        await self._exec(
            "INSERT INTO episode(anime_id, season, number, dub, message_id, file_size, "
            "duration, created_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(anime_id, season, number, dub) DO UPDATE SET "
            "message_id=excluded.message_id, file_size=excluded.file_size, "
            "duration=excluded.duration",
            (anime_id, season, number, dub, message_id, file_size, duration, now()),
        )
        row = await self._fetchone(
            "SELECT id FROM episode WHERE anime_id=? AND season=? AND number=? AND dub=?",
            (anime_id, season, number, dub),
        )
        return int(row["id"]) if row else 0

    def _ep(self, row: aiosqlite.Row) -> Episode:
        return Episode(
            row["id"], row["anime_id"], row["season"], row["number"],
            row["dub"], row["message_id"], row["file_size"], row["duration"],
        )

    async def get_episode(self, episode_id: int) -> Optional[Episode]:
        row = await self._fetchone("SELECT * FROM episode WHERE id = ?", (episode_id,))
        return self._ep(row) if row else None

    async def delete_episode(self, episode_id: int) -> None:
        await self._exec("DELETE FROM episode WHERE id = ?", (episode_id,))

    async def seasons(self, anime_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT DISTINCT season FROM episode WHERE anime_id = ? ORDER BY season", (anime_id,)
        )
        return [int(r["season"]) for r in rows]

    async def episode_numbers(self, anime_id: int, season: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT DISTINCT number FROM episode WHERE anime_id = ? AND season = ? "
            "ORDER BY number",
            (anime_id, season),
        )
        return [int(r["number"]) for r in rows]

    async def dubs(self, anime_id: int, season: int, number: int) -> list[Episode]:
        rows = await self._fetchall(
            "SELECT * FROM episode WHERE anime_id=? AND season=? AND number=? "
            "ORDER BY dub COLLATE NOCASE",
            (anime_id, season, number),
        )
        return [self._ep(r) for r in rows]

    async def find_episode(
        self, anime_id: int, season: int, number: int, dub: str
    ) -> Optional[Episode]:
        """Ищет серию в нужной озвучке, иначе — любую доступную."""
        row = await self._fetchone(
            "SELECT * FROM episode WHERE anime_id=? AND season=? AND number=? AND dub=?",
            (anime_id, season, number, dub),
        )
        if row:
            return self._ep(row)
        row = await self._fetchone(
            "SELECT * FROM episode WHERE anime_id=? AND season=? AND number=? LIMIT 1",
            (anime_id, season, number),
        )
        return self._ep(row) if row else None

    async def neighbour(self, ep: Episode, step: int) -> Optional[Episode]:
        """Соседняя серия (step=+1/-1), по возможности в той же озвучке."""
        if step > 0:
            row = await self._fetchone(
                "SELECT MIN(number) n FROM episode WHERE anime_id=? AND season=? AND number>?",
                (ep.anime_id, ep.season, ep.number),
            )
        else:
            row = await self._fetchone(
                "SELECT MAX(number) n FROM episode WHERE anime_id=? AND season=? AND number<?",
                (ep.anime_id, ep.season, ep.number),
            )
        if not row or row["n"] is None:
            return None
        return await self.find_episode(ep.anime_id, ep.season, int(row["n"]), ep.dub)

    async def count_episodes(self, anime_id: int | None = None) -> int:
        if anime_id is None:
            row = await self._fetchone("SELECT COUNT(*) c FROM episode")
        else:
            row = await self._fetchone(
                "SELECT COUNT(*) c FROM episode WHERE anime_id = ?", (anime_id,)
            )
        return int(row["c"]) if row else 0

    async def anime_summary(self, anime_id: int) -> tuple[int, int, list[str]]:
        """(сезонов, серий, список озвучек)"""
        seasons = await self.seasons(anime_id)
        total = await self.count_episodes(anime_id)
        rows = await self._fetchall(
            "SELECT DISTINCT dub FROM episode WHERE anime_id = ? ORDER BY dub", (anime_id,)
        )
        return len(seasons), total, [r["dub"] for r in rows]

    # ---------- тестовая подписка ----------

    async def trial_used(self, user_id: int) -> bool:
        row = await self._fetchone("SELECT trial_used FROM tg_user WHERE id = ?", (user_id,))
        return bool(row and row["trial_used"])

    async def mark_trial_used(self, user_id: int) -> None:
        await self._exec("UPDATE tg_user SET trial_used = 1 WHERE id = ?", (user_id,))

    async def reset_trial(self, user_id: int) -> None:
        await self._exec("UPDATE tg_user SET trial_used = 0 WHERE id = ?", (user_id,))

    # ---------- скидка, лежащая на пользователе ----------

    async def set_discount(self, user_id: int, percent: int, promo: str = "") -> None:
        await self._exec(
            "UPDATE tg_user SET discount = ?, discount_promo = ? WHERE id = ?",
            (percent, promo, user_id),
        )

    async def get_discount(self, user_id: int) -> tuple[int, str]:
        row = await self._fetchone(
            "SELECT discount, discount_promo FROM tg_user WHERE id = ?", (user_id,)
        )
        if not row:
            return 0, ""
        return int(row["discount"] or 0), row["discount_promo"] or ""

    async def clear_discount(self, user_id: int) -> None:
        await self.set_discount(user_id, 0, "")

    # ---------- промокоды ----------

    async def add_promo(
        self,
        code: str,
        kind: str,
        days: int = 0,
        percent: int = 0,
        max_uses: int = 0,
        expires_at: int = 0,
    ) -> int:
        return await self._exec(
            "INSERT INTO promo(code, kind, days, percent, max_uses, expires_at, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (code.upper(), kind, days, percent, max_uses, expires_at, now()),
        )

    async def get_promo(self, code: str) -> Optional[aiosqlite.Row]:
        return await self._fetchone("SELECT * FROM promo WHERE code = ?", (code.upper(),))

    async def promo_used_by(self, promo_id: int, user_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM promo_use WHERE promo_id = ? AND user_id = ?", (promo_id, user_id)
        )
        return row is not None

    async def use_promo(self, promo_id: int, user_id: int) -> None:
        await self._exec(
            "INSERT OR IGNORE INTO promo_use(promo_id, user_id, ts) VALUES(?,?,?)",
            (promo_id, user_id, now()),
        )
        await self._exec("UPDATE promo SET used = used + 1 WHERE id = ?", (promo_id,))

    async def list_promos(self, limit: int = 30) -> list[aiosqlite.Row]:
        return await self._fetchall("SELECT * FROM promo ORDER BY id DESC LIMIT ?", (limit,))

    async def delete_promo(self, promo_id: int) -> None:
        await self._exec("DELETE FROM promo_use WHERE promo_id = ?", (promo_id,))
        await self._exec("DELETE FROM promo WHERE id = ?", (promo_id,))

    # ---------- предложения ----------

    async def add_suggestion(self, title: str, norm: str) -> int:
        suggestion_id = await self._exec(
            "INSERT INTO suggestion(title, norm, created_at) VALUES(?,?,?)",
            (title, norm, now()),
        )
        await self.add_alias(suggestion_id, norm)
        return suggestion_id

    async def add_alias(self, suggestion_id: int, alias: str) -> None:
        if not alias:
            return
        await self._exec(
            "INSERT OR IGNORE INTO suggestion_alias(suggestion_id, alias) VALUES(?,?)",
            (suggestion_id, alias),
        )

    async def aliases_of(self, suggestion_id: int) -> list[str]:
        rows = await self._fetchall(
            "SELECT alias FROM suggestion_alias WHERE suggestion_id = ? ORDER BY alias",
            (suggestion_id,),
        )
        return [r["alias"] for r in rows]

    async def find_by_alias(self, alias: str) -> Optional[int]:
        row = await self._fetchone(
            "SELECT s.id FROM suggestion_alias a JOIN suggestion s ON s.id = a.suggestion_id "
            "WHERE a.alias = ? AND s.status != 'rejected' LIMIT 1",
            (alias,),
        )
        return int(row["id"]) if row else None

    async def vote(self, suggestion_id: int, user_id: int) -> bool:
        """Голос за предложение. False — этот человек уже голосовал."""
        if await self._fetchone(
            "SELECT 1 FROM suggestion_vote WHERE suggestion_id = ? AND user_id = ?",
            (suggestion_id, user_id),
        ):
            return False
        await self._exec(
            "INSERT INTO suggestion_vote(suggestion_id, user_id, ts) VALUES(?,?,?)",
            (suggestion_id, user_id, now()),
        )
        return True

    async def vote_exists(self, suggestion_id: int, user_id: int) -> bool:
        row = await self._fetchone(
            "SELECT 1 FROM suggestion_vote WHERE suggestion_id = ? AND user_id = ?",
            (suggestion_id, user_id),
        )
        return row is not None

    async def votes_of(self, suggestion_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) c FROM suggestion_vote WHERE suggestion_id = ?", (suggestion_id,)
        )
        return int(row["c"]) if row else 0

    async def get_suggestion(self, suggestion_id: int) -> Optional[aiosqlite.Row]:
        return await self._fetchone("SELECT * FROM suggestion WHERE id = ?", (suggestion_id,))

    async def open_suggestions(self) -> list[aiosqlite.Row]:
        """Все незакрытые предложения с их псевдонимами — для сопоставления."""
        return await self._fetchall(
            "SELECT s.id, s.title, s.norm, "
            "(SELECT COUNT(*) FROM suggestion_vote v WHERE v.suggestion_id = s.id) votes, "
            "(SELECT GROUP_CONCAT(a.alias, '|') FROM suggestion_alias a "
            " WHERE a.suggestion_id = s.id) aliases "
            "FROM suggestion s WHERE s.status = 'new'"
        )

    async def top_suggestions(self, limit: int = 20) -> list[aiosqlite.Row]:
        return await self._fetchall(
            "SELECT s.*, "
            "(SELECT COUNT(*) FROM suggestion_vote v WHERE v.suggestion_id = s.id) votes "
            "FROM suggestion s WHERE s.status = 'new' "
            "ORDER BY votes DESC, s.created_at LIMIT ?",
            (limit,),
        )

    async def set_suggestion_status(self, suggestion_id: int, status: str) -> None:
        await self._exec("UPDATE suggestion SET status = ? WHERE id = ?", (status, suggestion_id))

    async def merge_suggestions(self, src_id: int, dst_id: int) -> None:
        """Сливает одно предложение в другое: голоса и псевдонимы переезжают."""
        await self._exec(
            "INSERT OR IGNORE INTO suggestion_vote(suggestion_id, user_id, ts) "
            "SELECT ?, user_id, ts FROM suggestion_vote WHERE suggestion_id = ?",
            (dst_id, src_id),
        )
        await self._exec(
            "INSERT OR IGNORE INTO suggestion_alias(suggestion_id, alias) "
            "SELECT ?, alias FROM suggestion_alias WHERE suggestion_id = ?",
            (dst_id, src_id),
        )
        await self._exec("DELETE FROM suggestion_vote WHERE suggestion_id = ?", (src_id,))
        await self._exec("DELETE FROM suggestion_alias WHERE suggestion_id = ?", (src_id,))
        await self._exec("DELETE FROM suggestion WHERE id = ?", (src_id,))

    # ---------- статистика ----------

    async def stats(self) -> dict[str, int]:
        async def one(sql: str) -> int:
            row = await self._fetchone(sql)
            return int(row[0]) if row and row[0] is not None else 0

        return {
            "anime": await one("SELECT COUNT(*) FROM anime"),
            "episodes": await one("SELECT COUNT(*) FROM episode"),
            "users": await one("SELECT COUNT(*) FROM tg_user"),
            "banned": await one("SELECT COUNT(*) FROM tg_user WHERE banned = 1"),
            "subs": await one(f"SELECT COUNT(*) FROM tg_user WHERE sub_until > {now()}"),
            "views": await one("SELECT COUNT(*) FROM view"),
            "stars": await one("SELECT COALESCE(SUM(stars),0) FROM payment WHERE refunded = 0"),
            "day_users": await one(
                f"SELECT COUNT(*) FROM tg_user WHERE seen_at > {now() - 86400}"
            ),
            "day_views": await one(f"SELECT COUNT(*) FROM view WHERE ts > {now() - 86400}"),
        }
