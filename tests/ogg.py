"""Сборщик Ogg/Opus-файлов для тестов. CRC настоящий — exiftool такие читает."""

from __future__ import annotations

import struct

BOS, EOS, CONTINUED = 0x02, 0x04, 0x01
SILENCE = b"\xf8\xff\xfe"  # 20 мс тишины в Opus


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        r = i << 24
        for _ in range(8):
            r = (r << 1) ^ 0x04C11DB7 if r & 0x80000000 else r << 1
        table.append(r & 0xFFFFFFFF)
    return table


_TABLE = _crc_table()


def crc(data: bytes) -> int:
    value = 0
    for byte in data:
        value = ((value << 8) & 0xFFFFFFFF) ^ _TABLE[(value >> 24) ^ byte]
    return value


def _string(value: str | bytes) -> bytes:
    raw = value.encode() if isinstance(value, str) else value
    return struct.pack("<I", len(raw)) + raw


def opus_head() -> bytes:
    # версия, каналы, pre-skip, частота, усиление, раскладка каналов
    return b"OpusHead" + struct.pack("<BBHIhB", 1, 1, 312, 48000, 0, 0)


def opus_tags(vendor: str | bytes, comments: tuple[str, ...] = ()) -> bytes:
    return (
        b"OpusTags" + _string(vendor) + struct.pack("<I", len(comments))
        + b"".join(_string(c) for c in comments)
    )


def pages(packets: list[bytes], serial: int, seq: int = 0, *, bos: bool = False,
          eos: bool = False, granule: int = 0, max_segments: int = 255) -> list[bytes]:
    """Раскладывает пакеты по страницам. max_segments поменьше — режет пакет на страницы."""
    segments: list[bytes] = []
    for packet in packets:
        full = len(packet) // 255
        segments += [packet[i * 255:(i + 1) * 255] for i in range(full)]
        segments.append(packet[full * 255:])  # короче 255, бывает и пустым

    out: list[bytes] = []
    continued = False
    while segments:
        chunk, segments = segments[:max_segments], segments[max_segments:]
        lacing = bytes(len(s) for s in chunk)
        flags = (BOS if bos and not out else 0) | (EOS if eos and not segments else 0)
        flags |= CONTINUED if continued else 0
        ends = any(n < 255 for n in lacing)  # на странице кончается хоть один пакет
        page = bytearray(struct.pack(
            "<4sBBqIIIB", b"OggS", 0, flags, granule if ends else -1,
            serial, seq + len(out), 0, len(chunk),
        ) + lacing + b"".join(chunk))
        struct.pack_into("<I", page, 22, crc(bytes(page)))
        out.append(bytes(page))
        continued = lacing[-1] == 255
    return out


def voice(vendor: str | bytes, comments: tuple[str, ...] = (), *, serial: int = 0x5EED,
          max_segments: int = 255) -> bytes:
    """Голосовое как из клиента: OpusHead, OpusTags и немного звука."""
    head = pages([opus_head()], serial, 0, bos=True)
    tags = pages([opus_tags(vendor, comments)], serial, len(head), max_segments=max_segments)
    audio = pages([SILENCE] * 5, serial, len(head) + len(tags), eos=True, granule=312 + 960 * 5)
    return b"".join(head + tags + audio)
