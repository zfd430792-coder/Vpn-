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

import mp4build as mb
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

def video_note(size: int) -> VideoNote:
    return VideoNote(file_id="n1", file_unique_id="n1u", length=240, duration=9, file_size=size)


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

    print("\n[1] Голосовые")
    data = ogg.voice("libopus 1.5.1")
    msg = await send(data, voice=voice(len(data)))
    check("скачал файл", session.names()[:2] == ["GetFile", "download"], session.names())
    check("ответил: iPhone", msg and "✅ <b>iPhone</b>" in msg["text"], msg)
    check("показал Vendor", msg and "<code>libopus 1.5.1</code>" in msg["text"])
    check("ответ — реплаем на голосовое",
          msg and (msg.get("reply_parameters") or {}).get("message_id") == 10, msg)
    check("разметка HTML", msg and msg.get("parse_mode") == "HTML", msg)

    data = ogg.voice("libopus unknown-fixed")
    msg = await send(data, voice=voice(len(data)))
    check("Android", msg and "<b>Android</b>" in msg["text"], msg)

    data = (HERE / "ffmpeg.ogg").read_bytes()
    msg = await send(data, voice=voice(len(data)))
    check("другая версия Lavf → похоже на Desktop",
          msg and "🤔 Похоже на <b>Telegram Desktop</b>" in msg["text"], msg)

    data = ogg.voice("Recorder")
    msg = await send(data, voice=voice(len(data)))
    check("неизвестный клиент", msg and msg["text"].startswith("🤷"), msg)

    print("\n[2] Файлы вместо голосовых")
    data = ogg.voice("telegram-web-a")
    doc = Document(file_id="d1", file_unique_id="u2", file_name="voice.ogg",
                   mime_type="audio/ogg", file_size=len(data))
    msg = await send(data, document=doc)
    check(".ogg документом разбирается", msg and "Telegram Web A" in msg["text"], msg)

    audio = Audio(file_id="a1", file_unique_id="u3", duration=3, file_name="rec.opus",
                  mime_type="audio/opus", file_size=len(data))
    msg = await send(data, audio=audio)
    check(".opus аудиофайлом разбирается", msg and "Telegram Web A" in msg["text"], msg)

    pdf = Document(file_id="d2", file_unique_id="u4", file_name="report.pdf",
                   mime_type="application/pdf", file_size=1000)
    msg = await send(b"%PDF", document=pdf)
    check("PDF не качает", "GetFile" not in session.names(), session.names())
    check("PDF → подсказка", msg and msg["text"] == handlers.HELLO, msg)

    mp3 = Audio(file_id="a2", file_unique_id="u5", duration=3, file_name="song.mp3",
                mime_type="audio/mpeg", file_size=1000)
    msg = await send(b"ID3", audio=mp3)
    check("MP3 не качает", "GetFile" not in session.names(), session.names())

    print("\n[2.5] Кружки и видео")
    note_mp4 = (HERE / "note.mp4").read_bytes()
    msg = await send(note_mp4, video_note=video_note(len(note_mp4)))
    check("кружок скачан и разобран", session.names()[:2] == ["GetFile", "download"], session.names())
    check("ответ про кружок/видео", msg and "кружок или видео" in msg["text"], msg)
    check("честно про модель", msg and "не узнать" in msg["text"], msg)
    check("показал кодировщик", msg and "libx264" in msg["text"], msg)
    check("реплай на кружок", msg and (msg.get("reply_parameters") or {}).get("message_id") == 10, msg)

    built = mb.file(encoder="TGram")
    vid = Video(file_id="vv1", file_unique_id="vv1u", width=240, height=240, duration=9,
                mime_type="video/mp4", file_name="clip.mp4", file_size=len(built))
    msg = await send(built, video=vid)
    check("видео разбирается", msg and "<code>TGram</code>" in msg["text"], msg)

    doc = Document(file_id="dm1", file_unique_id="dm1u", file_name="circle.mp4",
                   mime_type="video/mp4", file_size=len(built))
    msg = await send(built, document=doc)
    check(".mp4 документом → разбор MP4", msg and "кружок или видео" in msg["text"], msg)

    msg = await send(b"not-an-mp4-at-all" + b"\x00" * 40, video_note=video_note(57))
    check("битый кружок → NOT_MP4", msg and msg["text"] == handlers.NOT_MP4, msg)

    print("\n[3] Ошибки")
    msg = await send(b"ID3\x04" + b"\x00" * 50, voice=voice(54))
    check("голосовое не Ogg/Opus", msg and msg["text"] == handlers.NOT_OPUS, msg)

    msg = await send(b"", voice=voice(30 * 1024 * 1024))
    check("больше 20 МБ — не качает", "GetFile" not in session.names(), session.names())
    check("больше 20 МБ — объясняет", msg and msg["text"] == handlers.TOO_BIG, msg)

    session.broken = True
    data = ogg.voice("libopus 1.5.1")
    msg = await send(data, voice=voice(len(data)))
    session.broken = False
    check("сеть упала при скачивании", msg and msg["text"] == handlers.DOWNLOAD_FAILED, msg)

    print("\n[4] Остальное")
    msg = await send(text="/start")
    check("/start → приветствие", msg and msg["text"] == handlers.HELLO, msg)
    msg = await send(text="привет")
    check("текст → подсказка", msg and msg["text"] == handlers.HELLO, msg)
    data = ogg.voice("libopus 1.5.1")
    await send(data, chat=GROUP, voice=voice(len(data)))
    check("в группах молчит", session.calls == [], session.names())

    await bot.session.close()


asyncio.run(main())
print()
if FAILS:
    print(f"❌ Провалено: {len(FAILS)}")
    sys.exit(1)
print("✅ Всё прошло")
