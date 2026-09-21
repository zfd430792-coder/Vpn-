"""Прогон настоящих апдейтов через диспетчер. Сеть подменена."""
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
from aiogram.methods import TelegramMethod
from aiogram.types import (CallbackQuery, Chat, Message, MessageId, Update, User)

from anibot import config, handlers, keyboards as kb, parser, search as se
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.userbot import Userbot

USER = User(id=555, is_bot=False, first_name="Тестер", username="tester")
ADMIN = User(id=777, is_bot=False, first_name="Админ", username="admin")
CHAT = Chat(id=555, type="private")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="anibot")


class MockSession(BaseSession):
    """Вместо HTTP — правдоподобные ответы. Записывает, что бот вызывал."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self._next_id = 1000

    async def close(self): pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        name = type(method).__name__
        payload = method.model_dump(exclude_none=True)
        self.calls.append((name, payload))
        self._next_id += 1
        if name in {"SendMessage", "EditMessageText", "SendVideo", "SendInvoice"}:
            return Message(
                message_id=self._next_id, date=datetime.now(), chat=CHAT,
                from_user=BOT_USER, text=payload.get("text", "") or "media",
            )
        if name == "CopyMessage":
            return MessageId(message_id=self._next_id)
        if name == "GetMe":
            return BOT_USER
        return True

    def last(self, name):
        for call_name, payload in reversed(self.calls):
            if call_name == name:
                return payload
        return None

    def names(self):
        return [c for c, _ in self.calls]


def cb(data: str, user=USER) -> Update:
    message = Message(
        message_id=10, date=datetime.now(), chat=CHAT, from_user=BOT_USER, text="меню"
    )
    return Update(
        update_id=1,
        callback_query=CallbackQuery(
            id="q1", from_user=user, chat_instance="ci", data=data, message=message
        ),
    )


def msg(text: str, user=USER) -> Update:
    return Update(
        update_id=1,
        message=Message(
            message_id=11, date=datetime.now(), chat=Chat(id=user.id, type="private"),
            from_user=user, text=text,
        ),
    )


FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


async def main():
    cfg = config.Config(
        bot_token="1:x", admins={777}, storage_channel=-1001234567890,
        data_dir=Path(os.environ["DATA_DIR"]),
    )
    db = Database(cfg.db_path)
    await db.connect()
    await db.set_setting("trial_enabled", "1")
    await db.set_setting("trial_days", "3")

    # наполняем каталог
    for cap, mid in [
        ("Тайтл Альфа\nСезон: 1\nСерия: 1\nОзвучка: Studio Band", 101),
        ("Тайтл Альфа\nСезон: 1\nСерия: 1\nОзвучка: AniLibria", 102),
        ("Тайтл Альфа\nСезон: 1\nСерия: 2\nОзвучка: Studio Band", 103),
    ]:
        p = parser.parse(cap)
        aid = await db.add_anime(p.title, se.normalize(p.title))
        await db.add_episode(aid, p.season, p.episode, p.dub, mid, 1_400_000_000, 1440)
    alpha = (await db.search_anime("тайтл альфа"))[0]

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

    print("\n[1] /start и меню")
    if await feed(msg("/start"), "/start"):
        sent = session.last("SendMessage")
        check("/start отвечает с меню", sent and "Аниме-бот" in sent["text"])
        check("у меню есть кнопки", sent and sent.get("reply_markup"))

    print("\n[2] Поиск по названию")
    if await feed(msg("альфа"), "поиск"):
        sent = session.last("SendMessage")
        check("нашёл единственный тайтл и открыл карточку",
              sent and "Тайтл Альфа" in sent["text"], sent and sent["text"][:60])

    print("\n[3] Навигация: тайтл → серии → озвучки")
    if await feed(cb(kb.Nav(to="anime", i=alpha.id).pack()), "открыть тайтл"):
        edited = session.last("EditMessageText")
        check("один сезон — сразу список серий", edited and "Серий" in edited["text"])
    if await feed(cb(kb.Nav(to="season", i=alpha.id, s=1).pack()), "список серий"):
        check("серии показаны", session.last("EditMessageText") is not None)
    if await feed(cb(kb.Nav(to="ep", i=alpha.id, s=1, e=1).pack()), "выбор озвучки"):
        edited = session.last("EditMessageText")
        check("две озвучки — спросил, какую",
              edited and "озвучку" in edited["text"].lower(), edited and edited["text"][:60])

    print("\n[4] Без подписки — пейволл с предложением теста")
    ep = await db.find_episode(alpha.id, 1, 1, "Studio Band")
    if await feed(cb(kb.Nav(to="watch", i=ep.id).pack()), "смотреть без подписки"):
        edited = session.last("EditMessageText")
        check("упёрлись в пейволл", edited and "подписка" in edited["text"].lower(),
              edited and edited["text"][:70])
        check("видео не ушло", session.last("CopyMessage") is None)
        buttons = [b["text"] for row in (edited or {}).get("reply_markup", {}).get(
            "inline_keyboard", []) for b in row]
        check("предложена тестовая подписка", any("Включить тест" in b for b in buttons), buttons)
        check("предложен промокод", any("промокод" in b.lower() for b in buttons), buttons)

    print("\n[5] Тестовая подписка открывает доступ")
    if await feed(cb(kb.Nav(to="trial").pack()), "включить тест"):
        check("тест включён", await db.has_sub(USER.id))
    if await feed(cb(kb.Nav(to="watch", i=ep.id).pack()), "смотреть после теста"):
        copied = session.last("CopyMessage")
        check("серия отправлена через copy_message", copied is not None)
        check("копия берётся из канала-хранилища",
              copied and copied["from_chat_id"] == cfg.storage_channel, copied)
        check("в подписи название и озвучка",
              copied and "Тайтл Альфа" in copied["caption"] and "Studio Band" in copied["caption"])
        check("включена защита от пересылки", copied and copied.get("protect_content") is True)
        check("просмотр записан", await db.views_count(USER.id) == 1)
    await db.revoke_sub(USER.id)
    if await feed(cb(kb.Nav(to="watch", i=ep.id).pack()), "тест кончился"):
        check("после теста снова пейволл", session.last("CopyMessage") is None)
        buttons = [b["text"] for row in (session.last("EditMessageText") or {}).get(
            "reply_markup", {}).get("inline_keyboard", []) for b in row]
        check("повторный тест не предлагают", not any("Включить тест" in b for b in buttons), buttons)

    print("\n[6] Подписка и счёт в звёздах")
    if await feed(cb(kb.Nav(to="subs").pack()), "витрина подписки"):
        edited = session.last("EditMessageText")
        check("витрина открылась", edited and "Подписка" in edited["text"])
    if await feed(cb(kb.Pay(plan="month").pack()), "выставить счёт"):
        inv = session.last("SendInvoice")
        check("счёт выставлен", inv is not None)
        check("валюта XTR (звёзды)", inv and inv["currency"] == "XTR", inv and inv.get("currency"))
        check("provider_token пустой", inv is not None and not inv.get("provider_token"))
        check("цена из настроек", inv and inv["prices"][0]["amount"] == 150, inv and inv["prices"])

    print("\n[7] После оплаты выдаётся подписка")
    from aiogram.types import SuccessfulPayment
    paid = Update(update_id=2, message=Message(
        message_id=12, date=datetime.now(), chat=CHAT, from_user=USER,
        successful_payment=SuccessfulPayment(
            currency="XTR", total_amount=150, invoice_payload="sub:month",
            telegram_payment_charge_id="ch_1", provider_payment_charge_id="",
        ),
    ))
    if await feed(paid, "оплата"):
        check("подписка активна", await db.has_sub(USER.id))
        check("платёж записан", (await db.stats())["stars"] == 150, (await db.stats())["stars"])
    if await feed(cb(kb.Nav(to="watch", i=ep.id).pack()), "смотреть после оплаты"):
        check("теперь серия отдаётся", session.last("CopyMessage") is not None)

    print("\n[8] Админка")
    if await feed(cb(kb.Nav(to="admin").pack(), user=ADMIN), "кнопка админки"):
        edited = session.last("EditMessageText")
        check("панель открылась", edited and "Админка" in edited["text"],
              edited and edited.get("text"))
    if await feed(cb(kb.Adm(act="titles").pack(), user=ADMIN), "список тайтлов"):
        check("тайтлы показаны", session.last("EditMessageText") is not None)
    if await feed(cb(kb.Adm(act="settings").pack(), user=ADMIN), "настройки"):
        edited = session.last("EditMessageText")
        check("настройки показаны", edited and "Настройки" in edited["text"])
    if await feed(cb(kb.Adm(act="titles").pack(), user=USER), "не-админ в админку"):
        check("обычного юзера в админку не пустило",
              session.last("EditMessageText") is None, session.names())

    print("\n[8б] Команд у бота нет")
    if await feed(msg("/admin", user=ADMIN), "набрал слеш"):
        sent = session.last("SendMessage")
        check("на слеш показывается меню",
              sent and "кнопками" in sent["text"], sent and sent.get("text"))
        check("это не админка", sent and "Админка" not in sent["text"])
    if await feed(msg("/чтоугодно"), "произвольный слеш"):
        sent = session.last("SendMessage")
        check("любой слеш ведёт в меню", sent and "Аниме-бот" in sent["text"],
              sent and sent.get("text"))

    print("\n[9] Бан")
    await db.set_banned(USER.id, True)
    if await feed(msg("альфа"), "забаненный пишет"):
        sent = session.last("SendMessage")
        check("забаненный получает отказ", sent and "закрыт" in sent["text"], sent)
    await db.set_banned(USER.id, False)

    await db.close()
    await bot.session.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Все сценарии прошли")


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
