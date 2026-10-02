"""Что лежит внутри MP4 — кружочка или видео — для опознания клиента.

У голосовых есть строка Vendor, и трюк из поста держится на ней. У видео её
нет: MP4 устроен иначе. Поэтому тут мы не ищем одно поле, а вытаскиваем всё,
что вообще может отличать один клиент Telegram от другого:

* бренды из ftyp (isom, mp42, …);
* кодеки дорожек (avc1, mp4a, …);
* имя кодировщика видео (поле compressorname, туда попадает сборка кодека);
* теги вроде ©too (кодировщик контейнера);
* названия обработчиков (hdlr).

Это те же данные, что показывает exiftool. Собрав их с реальных устройств,
можно понять, есть ли вообще разница по клиентам, и собрать таблицу — как для
голосовых.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# Боксы-контейнеры: внутрь заходим рекурсивно. ilst и meta разбираем отдельно.
_CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"edts"}
# Видеодорожки: у их sample-записей есть поле compressorname.
_VIDEO = {b"avc1", b"avc3", b"hvc1", b"hev1", b"mp4v", b"av01", b"vp08", b"vp09", b"s263"}
_MAX_BOXES = 20000  # потолок, чтобы не зациклиться на битом файле
_MAX_DEPTH = 12


class NotMp4(ValueError):
    """Файл не похож на MP4."""


@dataclass
class Mp4Info:
    brands: list[str] = field(default_factory=list)     # ftyp: major + совместимые
    codecs: list[str] = field(default_factory=list)     # avc1, mp4a, …
    handlers: list[str] = field(default_factory=list)   # описания из hdlr
    tags: dict[str, str] = field(default_factory=dict)  # ©too, Compressor, …

    def empty(self) -> bool:
        return not (self.brands or self.codecs or self.handlers or self.tags)


def _clean(raw: bytes) -> str:
    text = raw.decode("latin-1", "replace")
    return "".join(ch for ch in text if ch.isprintable()).strip()


def _cstr(raw: bytes) -> str:
    text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
    return "".join(ch for ch in text if ch.isprintable()).strip()


def _add(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def inspect(data: bytes) -> Mp4Info:
    if len(data) < 12 or data[4:8] != b"ftyp":
        raise NotMp4("нет сигнатуры ftyp")
    info = Mp4Info()
    _walk(data, 0, len(data), 0, info, [_MAX_BOXES])
    return info


def _walk(data: bytes, start: int, end: int, depth: int, info: Mp4Info, budget: list[int]) -> None:
    if depth > _MAX_DEPTH:
        return
    pos = start
    while pos + 8 <= end and budget[0] > 0:
        budget[0] -= 1
        size, typ = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:  # 64-битный размер
            if pos + 16 > end:
                break
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:  # до конца файла
            size = end - pos
        if size < header or pos + size > end:
            break
        _box(data, typ, pos + header, pos + size, depth, info, budget)
        pos += size


def _box(data: bytes, typ: bytes, body: int, end: int, depth: int, info: Mp4Info, budget: list[int]) -> None:
    if typ == b"ftyp":
        for idx in range((end - body) // 4):
            if idx == 1:  # minor version — не бренд
                continue
            _add(info.brands, _clean(data[body + idx * 4 : body + idx * 4 + 4]))
    elif typ == b"hdlr":
        if end - body >= 24:
            _add(info.handlers, _cstr(data[body + 24 : end]))
    elif typ == b"stsd":
        _stsd(data, body + 8, end, info)
    elif typ == b"meta":
        # ISO: 4 байта version/flags перед боксами; QuickTime — сразу боксы.
        inner = body if data[body + 4 : body + 8] == b"hdlr" else body + 4
        _walk(data, inner, end, depth + 1, info, budget)
    elif typ == b"ilst":
        _ilst(data, body, end, info)
    elif typ[:1] == b"\xa9":  # ©too и прочие теги в стиле QuickTime
        if end - body >= 4:
            length = struct.unpack_from(">H", data, body)[0]
            text = _cstr(data[body + 4 : body + 4 + length]) or _cstr(data[body + 4 : end])
            key = _clean(typ)
            if key and text:
                info.tags.setdefault(key, text)
    elif typ in _CONTAINERS:
        _walk(data, body, end, depth + 1, info, budget)


def _stsd(data: bytes, pos: int, end: int, info: Mp4Info) -> None:
    while pos + 8 <= end:
        size, fmt = struct.unpack_from(">I4s", data, pos)
        if size < 8 or pos + size > end:
            break
        _add(info.codecs, _clean(fmt))
        # compressorname — 32-байтовая Pascal-строка в видео-записи, смещение 50
        if fmt in _VIDEO and size >= 86:
            length = data[pos + 50]
            if 0 < length <= 31:
                name = _cstr(data[pos + 51 : pos + 51 + length])
                if name:
                    info.tags.setdefault("Compressor", name)
        pos += size


def _ilst(data: bytes, pos: int, end: int, info: Mp4Info) -> None:
    while pos + 8 <= end:
        size, tag = struct.unpack_from(">I4s", data, pos)
        if size < 8 or pos + size > end:
            break
        inner = pos + 8  # внутри записи — бокс data
        if inner + 16 <= pos + size:
            dsize, dtyp = struct.unpack_from(">I4s", data, inner)
            if dtyp == b"data" and 16 <= dsize and inner + dsize <= pos + size:
                key = _clean(tag)
                text = _cstr(data[inner + 16 : inner + dsize])
                if key and text:
                    info.tags.setdefault(key, text)
        pos += size
