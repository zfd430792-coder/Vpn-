"""Миграция старой базы: качество добавляется без потери данных."""
from pathlib import Path
import asyncio, sqlite3, sys, tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anibot.db import Database

FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


def make_old_db(path: Path) -> None:
    """База в том виде, в каком она была до появления качества."""
    old = sqlite3.connect(path)
    old.executescript("""
    CREATE TABLE anime (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
      norm TEXT NOT NULL, aliases TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
    CREATE UNIQUE INDEX idx_anime_norm ON anime(norm);
    CREATE TABLE episode (id INTEGER PRIMARY KEY AUTOINCREMENT, anime_id INTEGER NOT NULL,
      season INTEGER NOT NULL DEFAULT 1, number INTEGER NOT NULL, dub TEXT NOT NULL,
      message_id INTEGER NOT NULL, file_size INTEGER NOT NULL DEFAULT 0,
      duration INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
      UNIQUE(anime_id, season, number, dub));
    CREATE TABLE tg_user (id INTEGER PRIMARY KEY, username TEXT, name TEXT,
      joined_at INTEGER NOT NULL, seen_at INTEGER NOT NULL,
      banned INTEGER NOT NULL DEFAULT 0, sub_until INTEGER NOT NULL DEFAULT 0);
    INSERT INTO anime VALUES (1,'Старый Тайтл','старый тайтл','',100);
    INSERT INTO episode VALUES (1,1,1,1,'Studio Band',501,1400000000,1440,100);
    INSERT INTO episode VALUES (2,1,1,2,'Studio Band',502,1400000000,1440,100);
    INSERT INTO episode VALUES (3,1,1,1,'AniLibria',503,900000000,1440,100);
    INSERT INTO tg_user VALUES (42,'old','Старый',100,100,0,0);
    """)
    old.commit()
    old.close()


async def main():
    path = Path(tempfile.mkdtemp()) / "old.db"
    make_old_db(path)

    db = Database(path)
    await db.connect()
    try:
        print("\n[1] Данные пережили пересборку таблицы")
        check("тайтл на месте", await db.count_anime() == 1)
        check("все три серии на месте", await db.count_episodes() == 3,
              await db.count_episodes())
        check("пользователь на месте", await db.get_user(42) is not None)

        eps = await db.dubs(1, 1, 1)
        check("ссылки на сообщения целы",
              sorted(e.message_id for e in eps) == [501, 503],
              [e.message_id for e in eps])
        check("размеры целы", all(e.file_size for e in eps))
        check("старое считается 1080p", all(e.quality == 1080 for e in eps),
              [e.quality for e in eps])

        print("\n[2] Новое качество ложится рядом, а не поверх")
        await db.add_episode(1, 1, 1, "Studio Band", 777, quality=2160)
        variants = await db.qualities(1, 1, 1, "Studio Band")
        check("теперь два качества", len(variants) == 2,
              [(v.quality_name, v.message_id) for v in variants])
        check("лучшее первым", variants[0].quality == 2160)
        check("старый файл не тронут", variants[1].message_id == 501)

        print("\n[3] Повторная заливка того же качества перезаписывает")
        await db.add_episode(1, 1, 1, "Studio Band", 888, quality=2160)
        variants = await db.qualities(1, 1, 1, "Studio Band")
        check("дубля не появилось", len(variants) == 2, len(variants))
        check("ссылка обновилась", variants[0].message_id == 888, variants[0].message_id)

        print("\n[4] Озвучки не дублируются из-за качеств")
        names = await db.dub_names(1, 1, 1)
        check("озвучек ровно две", names == ["AniLibria", "Studio Band"], names)

        print("\n[5] Повторный запуск миграции безопасен")
        await db.close()
        db2 = Database(path)
        await db2.connect()
        check("данные на месте после второго открытия",
              await db2.count_episodes() == 4, await db2.count_episodes())
        await db2.close()
    finally:
        pass

    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Миграция безопасна")


def _run(coro):
    import os, traceback
    try:
        asyncio.run(coro)
    except SystemExit as exc:
        os._exit(exc.code or 0)
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


_run(main())
