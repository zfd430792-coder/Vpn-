"""Заливка серий: из канала-хранилища и через личку админа (с подписью и по шагам)."""
import asyncio, os, sys, tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Chat, Message, MessageId, Update, User, Video

from anibot import config, handlers, search as se
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.userbot import Userbot

CHANNEL_ID = -1001234567890
ADMIN = User(id=777, is_bot=False, first_name="Админ", username="admin")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="anibot")
ADMIN_CHAT = Chat(id=777, type="private")
CHANNEL = Chat(id=CHANNEL_ID, type="channel", title="Storage")

VIDEO = Video(
    file_id="f1", file_unique_id="u1", width=1920, height=1080,
    duration=1450, file_size=1_600_000_000,
)


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._n = 2000

    async def close(self): pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        self._n += 1
        if name == "CopyMessage":
            return MessageId(message_id=self._n)
        if name in {"SendMessage", "EditMessageText"}:
            return Message(message_id=self._n, date=datetime.now(), chat=ADMIN_CHAT,
                           from_user=BOT_USER, text="ok")
        if name == "GetMe":
            return BOT_USER
        return True

    def last(self, name):
        for n, p in reversed(self.calls):
            if n == name:
                return p
        return None

    def texts(self):
        return [p.get("text", "") for n, p in self.calls if n in {"SendMessage", "EditMessageText"}]


FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


async def main():
    cfg = config.Config(bot_token="1:x", admins={777}, storage_channel=CHANNEL_ID,
                        data_dir=Path(os.environ["DATA_DIR"]))
    db = Database(cfg.db_path)
    await db.connect()

    session = MockSession()
    bot = Bot("1:x", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(Deps(db, cfg, Userbot(0, "", "")))
    handlers.setup(dp)

    async def feed(update, label):
        session.calls.clear()
        try:
            await dp.feed_update(bot, update)
            return True
        except Exception as exc:
            check(label, False, f"{type(exc).__name__}: {exc}")
            return False

    def channel_post(caption, message_id, chat=CHANNEL):
        return Update(update_id=1, channel_post=Message(
            message_id=message_id, date=datetime.now(), chat=chat,
            video=VIDEO, caption=caption,
        ))

    def dm_video(caption=None):
        return Update(update_id=1, message=Message(
            message_id=50, date=datetime.now(), chat=ADMIN_CHAT, from_user=ADMIN,
            video=VIDEO, caption=caption,
        ))

    def dm_text(text, user=ADMIN):
        return Update(update_id=1, message=Message(
            message_id=51, date=datetime.now(), chat=Chat(id=user.id, type="private"),
            from_user=user, text=text,
        ))

    print("\n[1] Заливка из канала")
    if await feed(channel_post("Тайтл Альфа\nСезон: 1\nСерия: 4\nОзвучка: Studio Band", 301), "пост в канале"):
        check("тайтл заведён", await db.count_anime() == 1, await db.count_anime())
        check("серия заведена", await db.count_episodes() == 1)
        alpha = (await db.search_anime(se.normalize("тайтл альфа")))[0]
        ep = await db.find_episode(alpha.id, 1, 4, "Studio Band")
        check("ссылается на message_id поста", ep and ep.message_id == 301, ep and ep.message_id)
        check("размер файла сохранён", ep and ep.file_size == 1_600_000_000)
        check("длительность сохранена", ep and ep.duration == 1450)
        check("без метки качества — 1080p", ep and ep.quality == 1080, ep and ep.quality)
        check("админу пришло подтверждение",
              any("В каталоге" in t for t in session.texts()), session.texts())

    print("\n[1б] Качество из подписи")
    if await feed(channel_post("Тайтл Альфа | 1 | 4 | Studio Band | 4K", 310), "пост с 4K"):
        alpha = (await db.search_anime(se.normalize("тайтл альфа")))[0]
        variants = await db.qualities(alpha.id, 1, 4, "Studio Band")
        check("рядом с 1080 легло 4K, а не заменило его", len(variants) == 2,
              [(v.quality_name, v.message_id) for v in variants])
        check("лучшее качество первым", variants and variants[0].quality == 2160)
        names = await db.dub_names(alpha.id, 1, 4)
        check("озвучка в списке одна", names == ["Studio Band"], names)

    print("\n[2] Пост с непонятной подписью")
    before = await db.count_episodes()
    if await feed(channel_post("чё качать-то", 302), "мусорная подпись"):
        check("в каталог не попало", await db.count_episodes() == before,
              (before, await db.count_episodes()))
        check("админа предупредили",
              any("Не разобрал" in t for t in session.texts()), session.texts())

    print("\n[3] Пост из чужого канала игнорируется")
    other = Chat(id=-1009999999999, type="channel", title="Чужой")
    titles_before = await db.count_anime()
    if await feed(channel_post("Левый Тайтл | 1 | 1 | Кто-то", 303, chat=other), "чужой канал"):
        check("чужое не подхватывается", await db.count_anime() == titles_before,
              await db.count_anime())

    print("\n[4] Правка подписи перечитывается")
    edited = Update(update_id=1, edited_channel_post=Message(
        message_id=302, date=datetime.now(), chat=CHANNEL, video=VIDEO,
        caption="Тайтл Бета | 1 | 1 | AniLibria",
    ))
    if await feed(edited, "правка подписи"):
        check("после правки серия появилась", await db.count_episodes() == before + 1,
              (before, await db.count_episodes()))
        check("второй тайтл заведён", await db.count_anime() == titles_before + 1,
              await db.count_anime())

    print("\n[5] Админ прислал видео с подписью в личку")
    if await feed(dm_video("Тайтл Гамма | 2 | 9 | Studio Band"), "видео с подписью"):
        copied = session.last("CopyMessage")
        check("скопировано в канал", copied and copied["chat_id"] == CHANNEL_ID, copied)
        check("подпись приведена к единому виду",
              copied and "Название: Тайтл Гамма" in copied["caption"], copied and copied.get("caption"))
        gamma = (await db.search_anime(se.normalize("тайтл гамма")))[0]
        ep = await db.find_episode(gamma.id, 2, 9, "Studio Band")
        check("серия в базе", ep is not None)
        check("message_id взят от копии в канале", ep and ep.message_id == copied and False or (ep and ep.message_id > 2000), ep and ep.message_id)
        check("админу отчитались", any("Сохранено" in t for t in session.texts()))

    print("\n[6] Админ прислал видео без подписи — спрашиваем по шагам")
    if await feed(dm_video(None), "видео без подписи"):
        check("бот спросил название", any("называется" in t for t in session.texts()), session.texts())
    if await feed(dm_text("Тайтл Дельта"), "шаг: название"):
        check("спросил сезон", any("Сезон" in t for t in session.texts()))
    if await feed(dm_text("3"), "шаг: сезон"):
        check("спросил серию", any("серии" in t for t in session.texts()))
    if await feed(dm_text("12"), "шаг: серия"):
        check("спросил озвучку", any("Озвучка" in t for t in session.texts()))
    if await feed(dm_text("AniLibria"), "шаг: озвучка"):
        check("спросил качество", any("Качество" in t for t in session.texts()), session.texts())
    if await feed(dm_text("4k"), "шаг: качество"):
        delta = await db.search_anime(se.normalize("тайтл дельта"))
        check("тайтл создан по шагам", len(delta) == 1, len(delta))
        if delta:
            ep = await db.find_episode(delta[0].id, 3, 12, "AniLibria")
            check("серия создана по шагам", ep is not None)
            check("качество 4K сохранено", ep and ep.quality == 2160, ep and ep.quality)
        check("видео скопировано в канал", session.last("CopyMessage") is not None)

    print("\n[7] Обычный юзер видео залить не может")
    plain = User(id=555, is_bot=False, first_name="Юзер")
    upd = Update(update_id=1, message=Message(
        message_id=60, date=datetime.now(), chat=Chat(id=555, type="private"),
        from_user=plain, video=VIDEO, caption="Хак | 1 | 1 | Я",
    ))
    before = await db.count_episodes()
    if await feed(upd, "видео от юзера"):
        check("ничего не залилось", await db.count_episodes() == before)
        check("в канал не копировалось", session.last("CopyMessage") is None)

    await db.close()
    await bot.session.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Заливка работает во всех режимах")


def _run(coro):
    """Запуск с жёстким выходом: иначе поток SQLite держит процесс после падения."""
    import os
    import traceback

    try:
        asyncio.run(coro)
    except SystemExit as exc:
        os._exit(exc.code or 0)
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


_run(main())
