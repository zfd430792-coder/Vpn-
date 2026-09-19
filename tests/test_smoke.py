"""Смоук-тест: собираем всё как в бою, без обращений к сети."""
import asyncio, sys, tempfile, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from anibot import config, handlers, keyboards as kb, parser, search as se, service
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.userbot import Userbot, to_bot_id

FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)

async def main():
    print("\n[1] Сборка диспетчера и роутеров")
    dp = Dispatcher(storage=MemoryStorage())
    cfg = config.Config(bot_token="1:x", admins={777}, storage_channel=-1001234567890,
                        data_dir=Path(os.environ["DATA_DIR"]))
    db = Database(cfg.db_path)
    await db.connect()
    dp.update.outer_middleware(Deps(db, cfg, Userbot(0, "", "")))
    handlers.setup(dp)
    names = [r.name for r in dp.sub_routers]
    check("роутеры подключены в нужном порядке",
          names == ["admin", "subscription", "start", "watch", "catalog", "channel"], names)
    used = dp.resolve_used_update_types()
    check("бот слушает channel_post", "channel_post" in used, used)
    check("бот слушает pre_checkout_query", "pre_checkout_query" in used, used)

    print("\n[2] Каталог: заливка из подписей")
    captions = [
        ("Тайтл Альфа\nСезон: 1\nСерия: 1\nОзвучка: Studio Band", 101),
        ("Название: Тайтл Альфа\nСезон: 1\nСерия: 2\nОзвучка: Studio Band", 102),
        ("Тайтл Альфа | 1 | 2 | AniLibria", 103),
        ("Тайтл Альфа S02E01 [Studio Band]", 104),
        ("Тайтл Бета - 1 серия (AniLibria)", 105),
    ]
    saved = 0
    for cap, msg_id in captions:
        p = parser.parse(cap)
        if not p:
            continue
        aid = await db.add_anime(p.title, se.normalize(p.title))
        await db.add_episode(aid, p.season, p.episode, p.dub, msg_id, file_size=1_500_000_000, duration=1440)
        saved += 1
    check("разобрано и сохранено 5 подписей", saved == 5, saved)
    check("тайтлы не задвоились", await db.count_anime() == 2, await db.count_anime())
    check("серий в базе 5", await db.count_episodes() == 5, await db.count_episodes())

    alpha = (await db.search_anime(se.normalize("тайтл альфа")))[0]
    check("сезонов у Альфы: 2", await db.seasons(alpha.id) == [1, 2], await db.seasons(alpha.id))
    check("серий в 1 сезоне: 1,2", await db.episode_numbers(alpha.id, 1) == [1, 2])
    dubs = await db.dubs(alpha.id, 1, 2)
    check("у серии 1x02 две озвучки", len(dubs) == 2, [d.dub for d in dubs])

    print("\n[3] Поиск")
    check("точное совпадение", len(await db.search_anime(se.normalize("Тайтл Бета"))) == 1)
    check("поиск по куску", len(await db.search_anime(se.normalize("альфа"))) == 1)
    everything = await db.all_anime()
    order = se.rank("тайтл алфа", [(a.id, a.norm) for a in everything])  # опечатка
    check("нечёткий поиск переживает опечатку", order and order[0] == alpha.id, order)

    print("\n[4] Переходы и соседние серии")
    ep = await db.find_episode(alpha.id, 1, 1, "Studio Band")
    nxt = await db.neighbour(ep, +1)
    check("есть следующая серия", nxt is not None and nxt.number == 2)
    check("озвучка сохраняется при переходе", nxt.dub == "Studio Band", nxt.dub)
    check("за последней серией пусто", await db.neighbour(nxt, +1) is None)
    check("перед первой пусто", await db.neighbour(ep, -1) is None)

    print("\n[5] Доступ: бесплатная квота и подписка")
    await db.set_setting("free_episodes", "2")
    uid = 555
    await db.touch_user(uid, "u", "U")
    check("первая серия бесплатна", await service.has_access(db, cfg, uid, ep.id))
    await db.add_view(uid, ep.id)
    check("вторая бесплатна", await service.has_access(db, cfg, uid, nxt.id))
    await db.add_view(uid, nxt.id)
    third = await db.find_episode(alpha.id, 2, 1, "Studio Band")
    check("третья упирается в пейволл", not await service.has_access(db, cfg, uid, third.id))
    check("уже открытую пускает снова", await service.has_access(db, cfg, uid, ep.id))
    await db.grant_sub(uid, 30)
    check("с подпиской пускает", await service.has_access(db, cfg, uid, third.id))
    check("админа пускает всегда", await service.has_access(db, cfg, 777, third.id))

    print("\n[6] Клавиатуры и лимит callback_data в 64 байта")
    plans = await service.plans(db)
    markups = {
        "меню": kb.main_menu(True), "список": kb.anime_list(everything, 0, 3, "catalog"),
        "сезоны": kb.seasons(alpha.id, [1, 2]),
        "серии": kb.episodes(alpha.id, 1, list(range(1, 121)), 0),
        "озвучки": kb.dubs(alpha.id, 1, 2, dubs),
        "плеер": kb.player(ep, True, True, 2), "подписка": kb.subscription(plans, False),
        "админка": kb.admin_panel(), "тайтлы": kb.admin_titles(everything, 0, 2),
        "настройки": kb.admin_settings(2, True, 0), "цены": kb.admin_prices(plans),
    }
    worst = 0
    for name, markup in markups.items():
        for row in markup.inline_keyboard:
            for btn in row:
                worst = max(worst, len((btn.callback_data or "").encode()))
    check(f"все callback_data влезают (максимум {worst} Б)", worst <= 64, worst)
    check("сетка серий разбита по страницам",
          len(kb.episodes(alpha.id, 1, list(range(1, 121)), 0).inline_keyboard) <= 12)
    check("в плеере есть обе стрелки",
          sum("Пред" in b.text or "След" in b.text
              for r in markups["плеер"].inline_keyboard for b in r) == 2)

    print("\n[7] Тарифы и цены")
    check("четыре тарифа", len(plans) == 4, list(plans))
    await db.set_setting("price_month", "199")
    check("цена меняется из админки", (await service.plans(db))["month"][2] == 199)

    print("\n[8] Разное")
    check("id канала в формате Bot API", to_bot_id(1234567890) == -1001234567890)
    check("id уже в формате -100 не ломается", to_bot_id(-1001234567890) == -1001234567890)
    st = await db.stats()
    check("статистика считается", st["anime"] == 2 and st["episodes"] == 5 and st["users"] >= 1, st)
    from anibot import texts as t
    check("размер файла человекочитаемый", t.human_size(1_500_000_000) == "1.40 ГБ", t.human_size(1_500_000_000))
    check("длительность человекочитаемая", t.human_duration(1440) == "24:00", t.human_duration(1440))

    await db.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Все проверки прошли")

async def guarded():
    try:
        await main()
    finally:
        pass

asyncio.run(main())
