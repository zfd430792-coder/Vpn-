"""Служебные уведомления: предложения, платежи, статистика, логи.

Каждое назначение уходит в свой чат (или в свою тему служебной группы).
Если чат не настроен или недоступен — письмо всё равно дойдёт, просто
в личку админам.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from .config import Config
from .db import Database

log = logging.getLogger("anibot.notify")

SUGGESTIONS = "suggestions"
PAYMENTS = "payments"
STATS = "stats"
LOGS = "logs"
SUPPORT = "support"

HEARTBEAT_KEY = "last_seen"
HEARTBEAT_EVERY = 60  # секунд


async def send(bot: Bot, cfg: Config, purpose: str, text: str, **kwargs) -> bool:
    """Отправляет служебное сообщение. True — дошло хоть куда-то."""
    chat_id, thread_id = cfg.target(purpose)
    if chat_id:
        try:
            if thread_id:
                await bot.send_message(chat_id, text, message_thread_id=thread_id, **kwargs)
            else:
                await bot.send_message(chat_id, text, **kwargs)
            return True
        except TelegramAPIError as exc:
            log.warning("Служебный чат %s (%s) недоступен: %s", purpose, chat_id, exc)

    delivered = False
    for admin_id in cfg.admins:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, text, **kwargs)
            delivered = True
    return delivered


async def to_admins(bot: Bot, cfg: Config, text: str) -> None:
    for admin_id in cfg.admins:
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, text)


def human_gap(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек."
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин. {sec} сек."
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч. {minutes} мин."
    days, hours = divmod(hours, 24)
    return f"{days} дн. {hours} ч."


async def report_startup(bot: Bot, cfg: Config, db: Database) -> None:
    """Сообщает, что бот поднялся, и сколько он перед этим лежал.

    Метка времени обновляется раз в минуту, пока бот жив. Если при старте
    она старше двух минут — значит был простой, и его длительность и есть
    ответ на вопрос «сколько лежали».
    """
    now = int(time.time())
    last = await db.get_int_setting(HEARTBEAT_KEY, 0)
    await db.set_setting(HEARTBEAT_KEY, str(now))

    if not last:
        await send(bot, cfg, LOGS, "🟢 <b>Бот запущен</b>\nПервый запуск — простоя не было.")
        return

    gap = now - last
    if gap <= HEARTBEAT_EVERY * 2:
        await send(bot, cfg, LOGS, "🟢 <b>Бот перезапущен</b>\nПростой: меньше двух минут.")
        return

    await send(
        bot,
        cfg,
        LOGS,
        "🟢 <b>Бот снова на связи</b>\n"
        f"Лежал: <b>{human_gap(gap)}</b>\n"
        f"Последний признак жизни: {time.strftime('%d.%m %H:%M', time.localtime(last))}",
    )


async def heartbeat_loop(db: Database) -> None:
    """Раз в минуту отмечает, что бот жив. По этой метке считается простой."""
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_EVERY)
            await db.set_setting(HEARTBEAT_KEY, str(int(time.time())))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — сердцебиение не должно ронять бота
            log.warning("Не записал метку жизни: %s", exc)


async def report_shutdown(bot: Bot, cfg: Config) -> None:
    with contextlib.suppress(Exception):
        await send(bot, cfg, LOGS, "🔴 <b>Бот остановлен</b>\nШтатное завершение.")


class ErrorReporter(logging.Handler):
    """Шлёт ошибки уровня ERROR в служебный чат логов.

    Работает через очередь: логгер синхронный, а отправка — нет.
    Одинаковые ошибки схлопываются, чтобы не завалить чат.
    """

    def __init__(self, bot: Bot, cfg: Config, loop: asyncio.AbstractEventLoop):
        super().__init__(level=logging.ERROR)
        self.bot = bot
        self.cfg = cfg
        self.loop = loop
        self._recent: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()[:600]
            key = f"{record.name}:{record.lineno}"
            now = time.time()
            if now - self._recent.get(key, 0) < 300:  # то же самое не чаще раза в 5 минут
                return
            self._recent[key] = now
            text = (
                "🛠 <b>Ошибка</b>\n"
                f"<code>{record.name}</code>\n"
                f"<pre>{message}</pre>"
            )
            asyncio.run_coroutine_threadsafe(
                send(self.bot, self.cfg, LOGS, text), self.loop
            )
        except Exception:  # noqa: BLE001 — логгер не имеет права падать
            pass
