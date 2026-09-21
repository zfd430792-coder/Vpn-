"""Назначение каналов кнопкой из бота — работа без юзербота."""
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
from aiogram.types import (CallbackQuery, Chat, ChatMemberAdministrator,
                           ChatMemberLeft, ChatMemberUpdated, ForumTopic,
                           Message, MessageId, Update, User, Video)

from anibot import config, handlers
from anibot.__main__ import apply_saved_chats
from anibot.db import Database
from anibot.handlers.attach import Attach
from anibot.middlewares import Deps
from anibot.userbot import Userbot

MY_CHANNEL = -1007770000001
MY_GROUP = -1007770000002
ADMIN = User(id=777, is_bot=False, first_name="Админ", username="admin")
USER = User(id=555, is_bot=False, first_name="Юзер")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="anibot")
VIDEO = Video(file_id="f", file_unique_id="u", width=1920, height=1080,
              duration=1440, file_size=1_400_000_000)


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._n = 7000
        self._thread = 900
        self.forum_ok = True

    async def close(self): pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        from aiogram.exceptions import TelegramBadRequest
        name = type(method).__name__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        self._n += 1
        if name == "CreateForumTopic":
            if not self.forum_ok:
                raise TelegramBadRequest(method=method, message="TOPICS_DISABLED")
            self._thread += 1
            return ForumTopic(message_thread_id=self._thread, name="t", icon_color=0)
        if name == "GetChat":
            return Chat(id=MY_CHANNEL, type="channel", title="Моё Хранилище")
        if name == "CopyMessage":
            return MessageId(message_id=self._n)
        if name in {"SendMessage", "EditMessageText"}:
            return Message(message_id=self._n, date=datetime.now(),
                           chat=Chat(id=ADMIN.id, type="private"),
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


def admin_member(user=BOT_USER):
    return ChatMemberAdministrator(
        status="administrator", user=user, can_be_edited=False, is_anonymous=False,
        can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True,
        can_restrict_members=True, can_promote_members=False, can_change_info=True,
        can_invite_users=True, can_post_messages=True, can_manage_topics=True,
        can_post_stories=False, can_edit_stories=False, can_delete_stories=False,
        can_send_welcome_messages=False,
    )


async def main():
    # намеренно без юзербота и без канала в env — как при ручной заливке
    cfg = config.Config(bot_token="1:x", admins={777},
                        data_dir=Path(os.environ["DATA_DIR"]),
                        notify={})
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

    def added_to(chat_id, chat_type, title):
        return Update(update_id=1, my_chat_member=ChatMemberUpdated(
            chat=Chat(id=chat_id, type=chat_type, title=title),
            from_user=ADMIN, date=datetime.now(),
            old_chat_member=ChatMemberLeft(status="left", user=BOT_USER),
            new_chat_member=admin_member(),
        ))

    def cb(data, user=ADMIN):
        msg = Message(message_id=10, date=datetime.now(),
                      chat=Chat(id=user.id, type="private"), from_user=BOT_USER, text="м")
        return Update(update_id=1, callback_query=CallbackQuery(
            id="q", from_user=user, chat_instance="ci", data=data, message=msg))

    print("\n[1] Бот слушает своё добавление в чаты")
    check("my_chat_member в списке обновлений",
          "my_chat_member" in dp.resolve_used_update_types(),
          dp.resolve_used_update_types())

    print("\n[2] Добавили админом в канал — сообщил ID и предложил кнопку")
    if await feed(added_to(MY_CHANNEL, "channel", "Моё Хранилище"), "добавление в канал"):
        sent = session.last("SendMessage")
        check("написал админу", sent and sent["chat_id"] == ADMIN.id, sent)
        check("показал ID канала", sent and str(MY_CHANNEL) in sent["text"],
              sent and sent.get("text"))
        check("предложил сделать хранилищем",
              any("хранилищем" in b for b in session.buttons()), session.buttons())
        check("не предложил служебную группу для канала",
              not any("служебной группой" in b for b in session.buttons()), session.buttons())

    print("\n[3] Нажал кнопку — хранилище назначено без правки env")
    check("до нажатия хранилища нет", cfg.storage_channel == 0)
    if await feed(cb(Attach(role="storage", chat=MY_CHANNEL).pack()), "назначить хранилищем"):
        check("конфиг обновился на ходу", cfg.storage_channel == MY_CHANNEL, cfg.storage_channel)
        check("записано в базу",
              await db.get_int_setting("storage_channel", 0) == MY_CHANNEL)
        check("подтвердил и подсказал формат подписи",
              any("Сезон" in t for t in session.texts()), session.texts())

    print("\n[4] Заливка в этот канал сразу подхватывается")
    post = Update(update_id=1, channel_post=Message(
        message_id=555, date=datetime.now(),
        chat=Chat(id=MY_CHANNEL, type="channel", title="Моё Хранилище"),
        video=VIDEO, caption="Тайтл Альфа | 1 | 1 | Studio Band | 4K"))
    if await feed(post, "пост в назначенном канале"):
        check("тайтл заведён", await db.count_anime() == 1, await db.count_anime())
        check("серия заведена", await db.count_episodes() == 1)

    print("\n[5] Служебная группа: темы создаются")
    if await feed(added_to(MY_GROUP, "supergroup", "Служебная"), "добавление в группу"):
        check("предложил служебную группу",
              any("служебной группой" in b for b in session.buttons()), session.buttons())
    if await feed(cb(Attach(role="service", chat=MY_GROUP).pack()), "назначить группой"):
        check("темы созданы", len([c for c, _ in session.calls if c == "CreateForumTopic"]) == 4,
              [c for c, _ in session.calls])
        check("группа в конфиге", cfg.service_group == MY_GROUP)
        check("предложения адресованы в тему",
              cfg.notify.get("suggestions", (0, None))[1] is not None,
              cfg.notify.get("suggestions"))
        check("поддержка знает группу", cfg.notify.get("support", (0, None))[0] == MY_GROUP)

    print("\n[6] Если темы выключены — всё идёт в общий чат")
    # роутеры — одиночки и к двум диспетчерам не крепятся, поэтому просто
    # сбрасываем назначение и повторяем через тот же диспетчер
    session.forum_ok = False
    cfg.service_group = 0
    cfg.notify.clear()
    await db.set_setting("service_group", "0")
    if await feed(cb(Attach(role="service", chat=MY_GROUP).pack()), "группа без тем"):
        check("предупредил про выключенные темы",
              any("Темы не включились" in t for t in session.texts()), session.texts())
        check("адрес всё равно проставлен",
              cfg.notify.get("logs", (0, None))[0] == MY_GROUP, cfg.notify.get("logs"))
        check("без номера темы", cfg.notify.get("logs", (0, 1))[1] is None,
              cfg.notify.get("logs"))
        check("в базе адрес без темы",
              await db.get_setting("chat_logs") == str(MY_GROUP),
              await db.get_setting("chat_logs"))

    print("\n[7] После перезапуска назначенное подхватывается")
    fresh = config.Config(bot_token="1:x", admins={777},
                          data_dir=Path(os.environ["DATA_DIR"]), notify={})
    check("до чтения базы пусто", fresh.storage_channel == 0)
    await apply_saved_chats(db, fresh)
    check("хранилище восстановлено", fresh.storage_channel == MY_CHANNEL, fresh.storage_channel)

    print("\n[8] Обычный юзер назначить не может")
    before = cfg.storage_channel
    if await feed(cb(Attach(role="storage", chat=-1009999999999).pack(), user=USER),
                  "юзер пытается назначить"):
        check("хранилище не подменили", cfg.storage_channel == before, cfg.storage_channel)

    await db.close()
    await bot.session.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Назначение каналов из бота работает")


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
