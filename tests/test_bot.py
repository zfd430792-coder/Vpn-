"""Прогон настоящих апдейтов через диспетчер. Сеть подменена."""
import asyncio, sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (Audio, Chat, Document, File, Message, Update, User,
                           Video, VideoNote, Voice)

import ogg
from voicebot import handlers

USER = User(id=555, is_bot=False, first_name="Тестер")
PRIVATE = Chat(id=555, type="private")
GROUP = Chat(id=-100777, type="supergroup", title="Чат")
BOT_USER = User(id=1, is_bot=True, first_name="Bot", username="voicebot")

FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)


class MockSession(BaseSession):
    """Вместо HTTP — правдоподобные ответы. Записывает, что бот вызывал."""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.content = b""
        self.broken = False

    async def close(self): pass

    async def stream_content(self, url, *a, **kw):
        self.calls.append(("download", {"url": url}))
        if self.broken:
            raise aiohttp.ClientConnectionError("сеть упала")
        for i in range(0, len(self.content), 100):
            yield self.content[i:i + 100]

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        name = type(method).__name__
        payload = method.model_dump(exclude_none=True)
        self.calls.append((name, payload))
        if name == "GetFile":
            return File(file_id=payload["file_id"], file_unique_id="u",
                        file_size=len(self.content), file_path="voice/file_1.oga")
        if name == "SendMessage":
            return Message(message_id=99, date=datetime.now(), chat=PRIVATE,
                           from_user=BOT_USER, text=payload["text"])
        return True

    def names(self):
        return [name for name, _ in self.calls]

    def sent(self):
        return [p for name, p in self.calls if name == "SendMessage"]


def update(chat=PRIVATE, **fields) -> Update:
    message = Message(message_id=10, date=datetime.now(), chat=chat, from_user=USER, **fields)
    return Update(update_id=1, message=message)

def voice(size: int) -> Voice:
    return Voice(file_id="v1", file_unique_id="u1", duration=3, mime_type="audio/ogg", file_size=size)



async def main():
    session = MockSession()
    bot = Bot("1:x", session=session)
    dp = Dispatcher()
    dp.include_router(handlers.router)

    async def send(content: bytes = b"", **fields):
        session.calls.clear()
        session.content = content
        await dp.feed_update(bot, update(**fields))
        sent = session.sent()
        return sent[-1] if sent else None

    print("\n[1] Голосовые: в ответе только телефон")
    data = ogg.voice("libopus 1.5.1")
    msg = await send(data, voice=voice(len(data)))
    check("скачал файл", session.names()[:2] == ["GetFile", "download"], session.names())
    check("ответ ровно «iPhone»", msg and msg["text"] == "iPhone", msg)
    check("ответ — реплаем на голосовое",
          msg and (msg.get("reply_parameters") or {}).get("message_id") == 10, msg)
    check("одно сообщение на голосовое", len(session.sent()) == 1, session.names())

    for vendor, phone in [("libopus unknown-fixed", "Android"), ("libopus 1.3.1-fixed", "macOS"),
                          ("libopus unknown", "Telegram X"), ("tweb", "Telegram Web K")]:
        data = ogg.voice(vendor)
        msg = await send(data, voice=voice(len(data)))
        check(f"ответ ровно «{phone}»", msg and msg["text"] == phone, msg)

    data = (HERE / "ffmpeg.ogg").read_bytes()
    msg = await send(data, voice=voice(len(data)))
    check("другая версия Lavf → «Telegram Desktop»", msg and msg["text"] == "Telegram Desktop", msg)

    data = ogg.voice("Recorder")
    msg = await send(data, voice=voice(len(data)))
    check("неизвестная строка → «Неизвестно»", msg and msg["text"] == handlers.UNKNOWN, msg)

    print("\n[2] Всё, кроме голосовых, — молча")
    note = VideoNote(file_id="n1", file_unique_id="n1u", length=240, duration=9, file_size=5000)
    await send(video_note=note)
    check("кружок: ни ответа, ни скачивания", session.calls == [], session.names())

    vid = Video(file_id="vv1", file_unique_id="vv1u", width=240, height=240, duration=9, file_size=5000)
    await send(video=vid)
    check("видео: молчит", session.calls == [], session.names())

    doc = Document(file_id="d1", file_unique_id="u2", file_name="voice.ogg",
                   mime_type="audio/ogg", file_size=1000)
    await send(document=doc)
    check(".ogg файлом: молчит", session.calls == [], session.names())

    audio = Audio(file_id="a1", file_unique_id="u3", duration=3, file_name="song.mp3",
                  mime_type="audio/mpeg", file_size=1000)
    await send(audio=audio)
    check("аудиофайл: молчит", session.calls == [], session.names())

    await send(text="привет")
    check("текст: молчит", session.calls == [], session.names())

    data = ogg.voice("libopus 1.5.1")
    await send(data, chat=GROUP, voice=voice(len(data)))
    check("в группах молчит", session.calls == [], session.names())

    msg = await send(text="/start")
    check("/start → одна строка-подсказка", msg and msg["text"] == handlers.HELLO, msg)

    print("\n[3] Ошибки")
    msg = await send(b"ID3\x04" + b"\x00" * 50, voice=voice(54))
    check("голосовое не Ogg/Opus → «Неизвестно»", msg and msg["text"] == handlers.UNKNOWN, msg)

    msg = await send(b"", voice=voice(30 * 1024 * 1024))
    check("больше 20 МБ — не качает", "GetFile" not in session.names(), session.names())
    check("больше 20 МБ → «Неизвестно»", msg and msg["text"] == handlers.UNKNOWN, msg)

    session.broken = True
    data = ogg.voice("libopus 1.5.1")
    msg = await send(data, voice=voice(len(data)))
    session.broken = False
    check("сеть упала → просит переслать", msg and msg["text"] == handlers.DOWNLOAD_FAILED, msg)

    await bot.session.close()


asyncio.run(main())
print()
if FAILS:
    print(f"❌ Провалено: {len(FAILS)}")
    sys.exit(1)
print("✅ Всё прошло")
