"""Выбор качества и переписка с поддержкой — через настоящие апдейты."""
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
from aiogram.types import (CallbackQuery, Chat, ForumTopic, Message, MessageId,
                           Update, User)

from anibot import config, handlers, keyboards as kb, parser
from anibot.handlers.support import Sup
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.search import normalize
from anibot.userbot import Userbot

GROUP = -1005550000000
STORAGE = -1001234567890
USER = User(id=555, is_bot=False, first_name="Юзер", username="user")
ADMIN = User(id=777, is_bot=False, first_name="Админ", username="admin")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="anibot")


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._n = 4000
        self._thread = 500

    async def close(self): pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        self._n += 1
        if name == "CreateForumTopic":
            self._thread += 1
            return ForumTopic(message_thread_id=self._thread, name="тема", icon_color=0)
        if name == "CopyMessage":
            return MessageId(message_id=self._n)
        if name in {"SendMessage", "EditMessageText"}:
            return Message(message_id=self._n, date=datetime.now(),
                           chat=Chat(id=555, type="private"), from_user=BOT_USER, text="ok")
        if name == "GetMe":
            return BOT_USER
        return True

    def last(self, name):
        for n, p in reversed(self.calls):
            if n == name:
                return p
        return None

    def all_of(self, name):
        return [p for n, p in self.calls if n == name]

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
    cfg = config.Config(
        bot_token="1:x", admins={777}, storage_channel=STORAGE,
        data_dir=Path(os.environ["DATA_DIR"]), service_group=GROUP,
        notify={"support": (GROUP, None), "logs": (GROUP, 3)},
    )
    db = Database(cfg.db_path)
    await db.connect()
    await db.set_setting("trial_enabled", "0")

    aid = await db.add_anime("Тайтл Альфа", normalize("Тайтл Альфа"))
    # серия 1: две озвучки, у одной два качества
    await db.add_episode(aid, 1, 1, "Studio Band", 101, 1_400_000_000, 1440, 1080)
    await db.add_episode(aid, 1, 1, "Studio Band", 102, 6_000_000_000, 1440, 2160)
    await db.add_episode(aid, 1, 1, "AniLibria", 103, 1_300_000_000, 1440, 1080)
    # серия 2: одна озвучка, одно качество — лишних вопросов быть не должно
    await db.add_episode(aid, 1, 2, "Studio Band", 104, 1_400_000_000, 1440, 1080)

    session = MockSession()
    bot = Bot("1:x", session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(Deps(db, cfg, Userbot(0, "", "")))
    handlers.setup(dp)

    await db.grant_sub(USER.id, 30)  # доступ есть, проверяем именно навигацию

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
                      chat=Chat(id=user.id, type="private"), from_user=BOT_USER, text="меню")
        return Update(update_id=1, callback_query=CallbackQuery(
            id="q", from_user=user, chat_instance="ci", data=data, message=msg))

    def dm(text, user=USER):
        return Update(update_id=1, message=Message(
            message_id=11, date=datetime.now(), chat=Chat(id=user.id, type="private"),
            from_user=user, text=text))

    def in_topic(text, thread, user=ADMIN):
        return Update(update_id=1, message=Message(
            message_id=12, date=datetime.now(), chat=Chat(id=GROUP, type="supergroup"),
            from_user=user, text=text, message_thread_id=thread))

    print("\n[1] Озвучка -> качество")
    if await feed(cb(kb.Nav(to="ep", i=aid, s=1, e=1).pack()), "выбор озвучки"):
        check("спросил озвучку", any("озвучку" in t.lower() for t in session.texts()))
        check("обе озвучки в кнопках",
              sum("Studio Band" in b or "AniLibria" in b for b in session.buttons()) == 2,
              session.buttons())
        check("видео пока не ушло", session.last("CopyMessage") is None)

    rep = (await db.qualities(aid, 1, 1, "Studio Band"))[0]
    if await feed(cb(kb.Nav(to="dub", i=rep.id).pack()), "выбор качества"):
        check("спросил качество", any("качество" in t.lower() for t in session.texts()),
              session.texts())
        buttons = session.buttons()
        check("4K предложено", any("4K" in b for b in buttons), buttons)
        check("1080p предложено", any("1080p" in b for b in buttons), buttons)
        check("720 не предлагается", not any("720" in b for b in buttons), buttons)
        check("у кнопок виден размер", any("ГБ" in b for b in buttons), buttons)
        check("видео пока не ушло", session.last("CopyMessage") is None)

    best = (await db.qualities(aid, 1, 1, "Studio Band"))[0]
    if await feed(cb(kb.Nav(to="watch", i=best.id).pack()), "смотреть 4K"):
        copied = session.last("CopyMessage")
        check("серия отправлена", copied is not None)
        check("ушёл файл именно 4K", copied and copied["message_id"] == 102,
              copied and copied["message_id"])
        check("качество видно в подписи", copied and "4K" in copied["caption"],
              copied and copied.get("caption"))
        check("в плеере есть смена качества",
              any("Качество" in b for b in session.buttons()), session.buttons())

    print("\n[2] Одна озвучка и одно качество — без лишних вопросов")
    if await feed(cb(kb.Nav(to="ep", i=aid, s=1, e=2).pack()), "серия с одним вариантом"):
        copied = session.last("CopyMessage")
        check("сразу отдал видео", copied is not None and copied["message_id"] == 104,
              copied and copied.get("message_id"))
        check("не спрашивал про качество",
              not any("качество" in t.lower() for t in session.texts()), session.texts())
        check("в плеере нет кнопки качества",
              not any("Качество" in b for b in session.buttons()), session.buttons())

    print("\n[3] Соседняя серия держит озвучку и качество")
    nxt = await db.neighbour(best, +1)
    check("следующая найдена", nxt is not None and nxt.number == 2)
    check("озвучка сохранилась", nxt and nxt.dub == "Studio Band", nxt and nxt.dub)
    check("в следующей 4K нет — отдали лучшее из имеющегося",
          nxt and nxt.quality == 1080, nxt and nxt.quality)
    prev = await db.neighbour(nxt, -1)
    check("назад вернулись в ту же озвучку", prev and prev.dub == "Studio Band", prev and prev.dub)
    # осознанное поведение: листание сохраняет то качество, в котором смотрят
    # сейчас, а не прыгает обратно в 4K. Сменить можно кнопкой в плеере.
    check("и в то же качество, в котором смотрели", prev and prev.quality == 1080,
          prev and prev.quality)
    back_4k = await db.find_episode(aid, 1, 1, "Studio Band", 2160)
    check("4K остаётся доступным через смену качества",
          back_4k and back_4k.message_id == 102, back_4k)

    print("\n[4] Поддержка: человек пишет")
    await feed(cb(kb.Nav(to="support").pack()), "открыть поддержку")
    if await feed(dm("у меня не открывается серия"), "вопрос"):
        check("завели тему под этого человека", session.last("CreateForumTopic") is not None)
        copied = session.last("CopyMessage")
        check("вопрос ушёл в группу", copied and copied["chat_id"] == GROUP, copied)
        check("именно в свою тему", copied and copied.get("message_thread_id"), copied)
        check("человеку подтвердили", any("Отправлено" in t for t in session.texts()))
    ticket = await db.ensure_ticket(USER.id)
    thread = int(ticket["thread_id"])
    check("тема запомнена за пользователем", thread > 0, thread)

    if await feed(dm("и ещё вопрос"), "второе сообщение"):
        check("второй раз тему не создаём", session.last("CreateForumTopic") is None)
        copied = session.last("CopyMessage")
        check("ушло в ту же тему", copied and copied["message_thread_id"] == thread, copied)

    print("\n[5] Поддержка: админ отвечает в теме")
    if await feed(in_topic("попробуй перезайти", thread), "ответ админа"):
        sent = session.last("SendMessage")
        check("ответ ушёл именно этому человеку", sent and sent["chat_id"] == USER.id, sent)
        check("помечен как ответ поддержки", sent and "Ответ поддержки" in sent["text"],
              sent and sent.get("text"))

    print("\n[6] Чужие сообщения в группе бот не трогает")
    stranger = User(id=999, is_bot=False, first_name="Чужой")
    if await feed(in_topic("привет всем", thread, user=stranger), "не админ пишет в тему"):
        check("сообщение не-админа не пересылается",
              session.last("SendMessage") is None, session.calls)
    if await feed(in_topic("какой-то тайтл", 999999), "сообщение в чужой теме"):
        check("в теме без обращения бот молчит", not session.calls, session.calls)

    print("\n[7] Поиск не срабатывает в группе")
    group_msg = Update(update_id=1, message=Message(
        message_id=13, date=datetime.now(), chat=Chat(id=GROUP, type="supergroup"),
        from_user=ADMIN, text="Тайтл Альфа"))
    if await feed(group_msg, "название тайтла в группе"):
        check("бот не отвечает поиском в группе", not session.calls, session.calls)

    print("\n[8] Закрытие и ответ — кнопками, без команд")
    if await feed(cb(Sup(act="close", uid=USER.id).pack(), user=ADMIN), "кнопка закрытия"):
        row = await db.ensure_ticket(USER.id)
        check("обращение закрыто", row["status"] == "closed", row["status"])
        check("человеку сообщили",
              any("закрыто" in t for t in session.texts()), session.texts())
    if await feed(cb(Sup(act="close", uid=USER.id).pack(), user=USER), "юзер жмёт закрытие"):
        check("обычный юзер закрыть не может",
              not any("Обращение закрыто" in t for t in session.texts()), session.texts())

    if await feed(cb(Sup(act="reply", uid=USER.id).pack(), user=ADMIN), "кнопка ответа"):
        check("спросил текст ответа",
              any("Пришли ответ" in t for t in session.texts()), session.texts())
    if await feed(dm("держи ответ", user=ADMIN), "текст ответа"):
        sent = [p for p in session.all_of("SendMessage") if p["chat_id"] == USER.id]
        check("ответ дошёл", sent and "держи ответ" in sent[0]["text"], sent)

    await db.close()
    await bot.session.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Качество и поддержка работают")


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
