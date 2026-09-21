"""Промокоды, скидки и предложения — через настоящие апдейты, сеть подменена."""
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
from aiogram.types import CallbackQuery, Chat, Message, MessageId, Update, User

from anibot import config, handlers, keyboards as kb, service
from anibot.db import Database
from anibot.middlewares import Deps
from anibot.search import normalize
from anibot.userbot import Userbot

USER = User(id=555, is_bot=False, first_name="Юзер", username="user")
USER2 = User(id=556, is_bot=False, first_name="Второй", username="two")
ADMIN = User(id=777, is_bot=False, first_name="Админ", username="admin")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="anibot")


class MockSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self._n = 3000

    async def close(self): pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        name = type(method).__name__
        self.calls.append((name, method.model_dump(exclude_none=True)))
        self._n += 1
        if name == "CopyMessage":
            return MessageId(message_id=self._n)
        if name in {"SendMessage", "EditMessageText", "SendInvoice"}:
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

    def texts(self):
        return [p.get("text", "") for n, p in self.calls
                if n in {"SendMessage", "EditMessageText"}]

    def buttons(self):
        out = []
        for n, p in self.calls:
            markup = p.get("reply_markup") or {}
            for row in markup.get("inline_keyboard", []):
                out.extend(b["text"] for b in row)
        return out


FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


async def main():
    cfg = config.Config(bot_token="1:x", admins={777}, storage_channel=-1001234567890,
                        data_dir=Path(os.environ["DATA_DIR"]))
    db = Database(cfg.db_path)
    await db.connect()
    await db.set_setting("trial_enabled", "1")
    await db.set_setting("trial_days", "3")

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
                      chat=Chat(id=user.id, type="private"), from_user=BOT_USER, text="меню")
        return Update(update_id=1, callback_query=CallbackQuery(
            id="q", from_user=user, chat_instance="ci", data=data, message=msg))

    def txt(text, user=USER):
        return Update(update_id=1, message=Message(
            message_id=11, date=datetime.now(), chat=Chat(id=user.id, type="private"),
            from_user=user, text=text))

    print("\n[1] Промокод на подписку")
    await db.add_promo("FREE30", "sub", days=30)
    await feed(cb(kb.Nav(to="promo").pack()), "открыть промокод")
    if await feed(txt("free30"), "ввести код в нижнем регистре"):
        check("код принят независимо от регистра", await db.has_sub(USER.id))
        check("сообщение об успехе", any("Промокод принят" in t for t in session.texts()))
    await feed(cb(kb.Nav(to="promo").pack()), "открыть снова")
    if await feed(txt("FREE30"), "повторно тот же код"):
        check("повторно тот же код не проходит",
              any("уже использован" in t for t in session.texts()), session.texts())

    print("\n[2] Промокод на скидку и цена счёта")
    await db.add_promo("SALE50", "discount", percent=50)
    base = (await service.plans(db))["month"][2]
    await feed(cb(kb.Nav(to="promo").pack()), "открыть промокод")
    if await feed(txt("SALE50"), "ввести код скидки"):
        percent, code = await db.get_discount(USER.id)
        check("скидка записана", percent == 50 and code == "SALE50", (percent, code))
    if await feed(cb(kb.Pay(plan="month").pack()), "выставить счёт со скидкой"):
        inv = session.last("SendInvoice")
        check("цена уменьшилась вдвое", inv and inv["prices"][0]["amount"] == round(base / 2),
              inv and inv["prices"][0]["amount"])

    print("\n[3] Несуществующий и исчерпанный код")
    await feed(cb(kb.Nav(to="promo").pack()), "открыть")
    if await feed(txt("НЕТТАКОГО"), "неизвестный код"):
        check("неизвестный код отклонён", any("нет" in t.lower() for t in session.texts()))
    await db.add_promo("ONCE", "sub", days=7, max_uses=1)
    promo = await db.get_promo("ONCE")
    await db.use_promo(promo["id"], 99999)
    await feed(cb(kb.Nav(to="promo").pack()), "открыть")
    if await feed(txt("ONCE"), "исчерпанный код"):
        check("исчерпанный код отклонён",
              any("лимит" in t.lower() for t in session.texts()), session.texts())

    print("\n[4] Предложения: новое, голос, аббревиатура")
    await feed(cb(kb.Nav(to="suggest").pack()), "открыть предложения")
    if await feed(txt("Клинок Рассекающий Демонов"), "первое предложение"):
        check("предложение записано", any("Записал" in t for t in session.texts()),
              session.texts())
    rows = await db.top_suggestions()
    check("в списке одно предложение", len(rows) == 1, len(rows))
    check("у него один голос", rows and rows[0]["votes"] == 1, rows and rows[0]["votes"])

    await feed(cb(kb.Nav(to="suggest").pack(), user=USER2), "второй открыл")
    if await feed(txt("крд", user=USER2), "второй пишет аббревиатуру"):
        rows = await db.top_suggestions()
        check("аббревиатура попала в тот же тайтл, а не создала новый",
              len(rows) == 1, [r["title"] for r in rows])
        check("голосов стало два", rows and rows[0]["votes"] == 2, rows and rows[0]["votes"])
        check("ответ про учтённый голос",
              any("Голос учтён" in t for t in session.texts()), session.texts())

    if await feed(txt("клинок", user=USER2), "тот же человек пишет сокращение"):
        rows = await db.top_suggestions()
        check("повторный голос не удваивается", rows[0]["votes"] == 2, rows[0]["votes"])
        check("сказали, что уже голосовал",
              any("уже голосовал" in t for t in session.texts()), session.texts())

    print("\n[5] Спорное — бот спрашивает, а не гадает")
    await db.add_suggestion("Лунный Клинов", normalize("Лунный Клинов"))
    await db.add_suggestion("Лунный Клинок", normalize("Лунный Клинок"))
    user3 = User(id=558, is_bot=False, first_name="Третий")
    await feed(cb(kb.Nav(to="suggest").pack(), user=user3), "третий открыл")
    if await feed(txt("лунный клино", user=user3), "спорный ввод"):
        check("бот показал варианты",
              any("Уточни" in t for t in session.texts()), session.texts())
        picks = [b for b in session.buttons() if "Лунный" in b]
        check("в вариантах оба похожих", len(picks) >= 2, session.buttons())

    before = len(await db.top_suggestions())
    target = [r for r in await db.top_suggestions() if r["title"] == "Лунный Клинок"][0]
    if await feed(cb(kb.Nav(to="sgpick", i=target["id"]).pack(), user=user3), "выбрал вариант"):
        check("новых предложений не создалось", len(await db.top_suggestions()) == before)
        check("голос ушёл в выбранный", await db.votes_of(target["id"]) == 1)
        aliases = await db.aliases_of(target["id"])
        check("написание запомнилось псевдонимом", "лунный клино" in aliases, aliases)

    user4 = User(id=559, is_bot=False, first_name="Четвёртый")
    await feed(cb(kb.Nav(to="suggest").pack(), user=user4), "четвёртый открыл")
    if await feed(txt("лунный клино", user=user4), "то же написание после обучения"):
        check("теперь засчитывается без вопросов",
              any("Голос учтён" in t for t in session.texts()), session.texts())
        check("голосов стало два", await db.votes_of(target["id"]) == 2)

    print("\n[6] Админ создаёт промокод по шагам")
    await feed(cb(kb.Adm(act="promo_new").pack(), user=ADMIN), "новый промокод")
    await feed(cb(kb.Adm(act="promo_kind_disc").pack(), user=ADMIN), "тип: скидка")
    await feed(txt("25", user=ADMIN), "процент")
    await feed(txt("WINTER25", user=ADMIN), "код")
    if await feed(txt("10", user=ADMIN), "лимит"):
        created = await db.get_promo("WINTER25")
        check("промокод создан", created is not None)
        check("процент верный", created and created["percent"] == 25, created and created["percent"])
        check("лимит верный", created and created["max_uses"] == 10)

    print("\n[6б] Возврат звёзд кнопками")
    pay_id = await db.add_payment(USER2.id, "month", 150, "charge_test")
    await db.grant_sub(USER2.id, 30)
    if await feed(cb(kb.Adm(act="payments").pack(), user=ADMIN), "список платежей"):
        check("платежи показаны", any("Последние платежи" in t for t in session.texts()))
        check("платёж кнопкой", any("150⭐" in b for b in session.buttons()), session.buttons())
    if await feed(cb(kb.Adm(act="pay_one", arg=pay_id).pack(), user=ADMIN), "карточка платежа"):
        check("карточка открылась", any(f"Платёж #{pay_id}" in t for t in session.texts()),
              session.texts())
        check("есть кнопка возврата", any("Вернуть звёзды" in b for b in session.buttons()),
              session.buttons())
    if await feed(cb(kb.Adm(act="refund_ask", arg=pay_id).pack(), user=ADMIN), "спросил подтверждение"):
        check("спросил подтверждение", any("Вернуть <b>150</b>" in t for t in session.texts()),
              session.texts())
    if await feed(cb(kb.Adm(act="refund_do", arg=pay_id).pack(), user=ADMIN), "возврат"):
        check("вызван возврат у Telegram",
              any(n == "RefundStarPayment" for n, _ in session.calls),
              [n for n, _ in session.calls])
        check("подписка снята", not await db.has_sub(USER2.id))
        rows = [r for r in await db.last_payments(50) if r["id"] == pay_id]
        check("платёж помечен возвращённым", rows and rows[0]["refunded"] == 1,
              rows and rows[0]["refunded"])
    if await feed(cb(kb.Adm(act="refund_do", arg=pay_id).pack(), user=ADMIN), "повторный возврат"):
        check("повторно не возвращает",
              not any(n == "RefundStarPayment" for n, _ in session.calls),
              [n for n, _ in session.calls])

    print("\n[6в] Заливка с диска без юзербота")
    if await feed(cb(kb.Adm(act="pull").pack(), user=ADMIN), "кнопка заливки с диска"):
        check("честно отказался, а не завис",
              not any("путь к файлу" in t for t in session.texts()), session.texts())

    print("\n[7] Админ видит предложения")
    if await feed(cb(kb.Adm(act="sugg").pack(), user=ADMIN), "список предложений"):
        check("список показан", any("Предложения" in t for t in session.texts()))
    top = (await db.top_suggestions())[0]
    if await feed(cb(kb.Adm(act="sugg_one", arg=top["id"]).pack(), user=ADMIN), "карточка"):
        check("в карточке видно, как писали",
              any("Как писали" in t for t in session.texts()), session.texts())

    await db.close()
    await bot.session.close()
    print("\n" + "─" * 46)
    if FAILS:
        print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("✅ Промокоды и предложения работают")


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
