"""Заявки «сообщить, когда появится»: авто-сверка при заливке и кнопка проверки."""
from pathlib import Path
import asyncio, os, sys, tempfile
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, Chat, Message, MessageId, Update,
                           User, Video)

from anibot import config, handlers, keyboards as kb, watchlist
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.search import normalize
from anibot.userbot import Userbot

CHANNEL = -1001234567890
USER = User(id=555, is_bot=False, first_name="Юзер", username="user")
USER2 = User(id=556, is_bot=False, first_name="Второй", username="two")
ADMIN = User(id=777, is_bot=False, first_name="Админ", username="admin")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="anibot")
VIDEO = Video(file_id="f", file_unique_id="u", width=1920, height=1080,
              duration=1440, file_size=1_400_000_000)


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._n = 6000

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
            return Message(message_id=self._n, date=datetime.now(),
                           chat=Chat(id=555, type="private"), from_user=BOT_USER, text="ok")
        if name == "GetMe":
            return BOT_USER
        return True

    def to(self, chat_id):
        return [p for n, p in self.calls if n == "SendMessage" and p.get("chat_id") == chat_id]

    def texts(self):
        return [p.get("text", "") for n, p in self.calls
                if n in {"SendMessage", "EditMessageText"}]

    def buttons(self):
        out = []
        for _n, p in self.calls:
            for row in (p.get("reply_markup") or {}).get("inline_keyboard", []):
                out.extend(b["text"] for b in row)
        return out


FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


async def main():
    cfg = config.Config(bot_token="1:x", admins={777}, storage_channel=CHANNEL,
                        data_dir=Path(os.environ["DATA_DIR"]))
    db = Database(cfg.db_path)
    await db.connect()
    await db.set_setting("trial_enabled", "0")

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

    def cb(data, user=USER):
        msg = Message(message_id=10, date=datetime.now(),
                      chat=Chat(id=user.id, type="private"), from_user=BOT_USER, text="м")
        return Update(update_id=1, callback_query=CallbackQuery(
            id="q", from_user=user, chat_instance="ci", data=data, message=msg))

    def dm(text, user=USER):
        return Update(update_id=1, message=Message(
            message_id=11, date=datetime.now(), chat=Chat(id=user.id, type="private"),
            from_user=user, text=text))

    def post(caption, mid):
        return Update(update_id=1, channel_post=Message(
            message_id=mid, date=datetime.now(),
            chat=Chat(id=CHANNEL, type="channel", title="S"),
            video=VIDEO, caption=caption))

    print("\n[1] Поиск не нашёл — предлагается кнопка")
    if await feed(dm("Клинок Рассекающий Демонов"), "поиск по пустому каталогу"):
        check("сказали, что не нашлось", any("ничего не нашлось" in t for t in session.texts()))
        check("есть кнопка уведомления",
              any("Сообщить, когда появится" in b for b in session.buttons()),
              session.buttons())

    print("\n[2] Нажал кнопку — записался и проголосовал")
    if await feed(cb(kb.Nav(to="wantit").pack()), "подписаться"):
        check("подтвердили подписку", any("Сообщу" in t for t in session.texts()),
              session.texts())
    check("заявка в базе", await db.has_watch(USER.id, normalize("Клинок Рассекающий Демонов")))
    sugg = await db.top_suggestions()
    check("предложение тоже завелось", len(sugg) == 1, [s["title"] for s in sugg])
    check("голос засчитан", sugg and sugg[0]["votes"] == 1)

    print("\n[3] Второй человек просит то же сокращением")
    await feed(cb(kb.Nav(to="suggest").pack(), user=USER2), "открыть предложения")
    if await feed(dm("крд", user=USER2), "аббревиатура"):
        check("голос ушёл в тот же тайтл", len(await db.top_suggestions()) == 1)
        check("второй тоже ждёт", await db.count_open_watches() == 2,
              await db.count_open_watches())

    print("\n[4] Залили тайтл — уведомления ушли сами")
    if await feed(post("Клинок Рассекающий Демонов | 1 | 1 | Studio Band", 701), "заливка"):
        first = session.to(USER.id)
        second = session.to(USER2.id)
        check("первому пришло", first and "Появилось" in first[0]["text"], first)
        check("второму пришло", second and "Появилось" in second[0]["text"], second)
        check("в уведомлении есть кнопка «Смотреть»",
              any("Смотреть" in b for b in session.buttons()), session.buttons())
        check("видно, как человек просил",
              first and "крд" not in first[0]["text"], first and first[0]["text"])

    check("заявок в ожидании не осталось", await db.count_open_watches() == 0,
          await db.count_open_watches())

    print("\n[5] Повторная заливка не спамит")
    if await feed(post("Клинок Рассекающий Демонов | 1 | 2 | Studio Band", 702), "вторая серия"):
        check("никого не дёрнули повторно", not session.to(USER.id) and not session.to(USER2.id),
              session.texts())

    print("\n[6] Кнопка «Проверить тайтлы» в админке")
    # заявка на тайтл, который уже лежит в каталоге, но уведомление не уходило
    await db.add_anime("Магическая Битва", normalize("Магическая Битва"))
    mb = (await db.search_anime(normalize("магическая битва")))[0]
    await db.add_episode(mb.id, 1, 1, "AniLibria", 800)
    await db.add_watch(USER.id, "магическая битва", normalize("магическая битва"))
    await db.add_watch(USER2.id, "мб", normalize("мб"))
    check("две заявки ждут", await db.count_open_watches() == 2)

    if await feed(cb(kb.Adm(act="scan").pack(), user=ADMIN), "проверить тайтлы"):
        report = [t for t in session.texts() if "Проверка каталога" in t]
        check("показан отчёт", bool(report), session.texts())
        check("нашлись обе заявки", report and "Нашлось в каталоге: <b>2</b>" in report[0],
              report and report[0])
        check("людям ушли уведомления",
              bool(session.to(USER.id)) and bool(session.to(USER2.id)))
    check("после проверки ожидающих нет", await db.count_open_watches() == 0,
          await db.count_open_watches())

    print("\n[7] Не-админ проверку не запускает")
    if await feed(cb(kb.Adm(act="scan").pack(), user=USER), "проверка обычным юзером"):
        check("обычного юзера не пустило",
              not any("Проверка каталога" in t for t in session.texts()), session.texts())

    print("\n[8] Сопоставление не приклеивает чужое")
    await db.add_watch(USER.id, "совсем другое аниме", normalize("совсем другое аниме"))
    found, sent, _closed = await watchlist.full_scan(bot, db)
    check("чужая заявка не закрылась", found == 0 and sent == 0, (found, sent))
    check("она осталась в ожидании", await db.count_open_watches() == 1)

    await db.close()
    await bot.session.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Уведомления о появлении работают")


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
