"""Строка Vendor из голосового сообщения — то же, что показывает exiftool.

Голосовое в Telegram — это Ogg-контейнер с кодеком Opus. Вторым пакетом
потока идёт заголовок OpusTags (RFC 7845, §5.2), и первым полем в нём —
строка вендора: имя сборки кодировщика, которым записан звук. У каждого
клиента Telegram сборка своя, отсюда и весь фокус.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

# Заголовок Ogg-страницы, 27 байт: сигнатура, версия, флаги, позиция,
# номер потока, номер страницы, CRC, число сегментов.
_PAGE = struct.Struct("<4sBBqIIIB")
_CONTINUED = 0x01  # первый пакет страницы — продолжение пакета с прошлой


class NotOpus(ValueError):
    """Файл не Ogg/Opus или обрезан раньше, чем кончились заголовки."""


def _packets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Собирает пакеты из Ogg-страниц: (номер потока, пакет)."""
    pos = 0
    unfinished: dict[int, bytearray] = {}
    while pos < len(data):
        if len(data) - pos < _PAGE.size:
            raise NotOpus("файл обрезан")
        magic, _ver, flags, _granule, serial, _seq, _crc, count = _PAGE.unpack_from(data, pos)
        if magic != b"OggS":
            raise NotOpus("это не Ogg")
        body = pos + _PAGE.size + count
        lacing = data[pos + _PAGE.size : body]
        pos = body + sum(lacing)
        if len(lacing) < count or pos > len(data):
            raise NotOpus("файл обрезан")

        tail = unfinished.pop(serial, bytearray())
        packet = tail if flags & _CONTINUED else bytearray()
        for size in lacing:
            packet += data[body : body + size]
            body += size
            if size < 255:  # сегмент короче 255 байт закрывает пакет
                yield serial, bytes(packet)
                packet = bytearray()
        if packet:  # пакет переходит на следующую страницу
            unfinished[serial] = packet


def read_vendor(data: bytes) -> str:
    """Возвращает строку Vendor или бросает NotOpus."""
    opus_serial = None
    for serial, packet in _packets(data):
        if opus_serial is None:
            if packet.startswith(b"OpusHead"):
                opus_serial = serial
            continue
        if serial != opus_serial:
            continue
        if not packet.startswith(b"OpusTags"):
            raise NotOpus("за OpusHead нет OpusTags")
        start = len(b"OpusTags") + 4
        if len(packet) < start:
            raise NotOpus("OpusTags обрезан")
        (length,) = struct.unpack_from("<I", packet, start - 4)
        if start + length > len(packet):
            raise NotOpus("OpusTags обрезан")
        return packet[start : start + length].decode("utf-8", errors="replace")
    raise NotOpus("нет Opus-потока" if opus_serial is None else "нет OpusTags")
