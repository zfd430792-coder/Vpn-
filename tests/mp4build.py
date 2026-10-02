"""Мини-сборщик MP4 для тестов: только то, что читает инспектор."""

from __future__ import annotations

import struct


def box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def container(typ: bytes, *children: bytes) -> bytes:
    return box(typ, b"".join(children))


def ftyp(major: bytes = b"isom", *compatible: bytes) -> bytes:
    return box(b"ftyp", major + b"\x00\x00\x00\x00" + b"".join(compatible))


def hdlr(handler_type: bytes, name: str) -> bytes:
    # version/flags(4) + pre_defined(4) + handler_type(4) + reserved(12) + name\0
    body = b"\x00" * 4 + b"\x00" * 4 + handler_type + b"\x00" * 12 + name.encode() + b"\x00"
    return box(b"hdlr", body)


def visual_entry(fmt: bytes, compressor: str) -> bytes:
    name = compressor.encode()[:31]
    pascal = bytes([len(name)]) + name + b"\x00" * (31 - len(name))  # 32 байта
    payload = b"\x00" * 42 + pascal + b"\x00" * 4  # compressorname на смещении 50
    return box(fmt, payload)


def audio_entry(fmt: bytes = b"mp4a") -> bytes:
    return box(fmt, b"\x00" * 28)


def stsd(*entries: bytes) -> bytes:
    return box(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", len(entries)) + b"".join(entries))


def ilst_tag(tag: bytes, value: str) -> bytes:
    data = box(b"data", b"\x00\x00\x00\x01" + b"\x00\x00\x00\x00" + value.encode())
    return box(tag, data)


def meta_iso(*children: bytes) -> bytes:
    return box(b"meta", b"\x00\x00\x00\x00" + b"".join(children))


def meta_quicktime(*children: bytes) -> bytes:
    # без version/flags: первым сразу идёт hdlr
    return box(b"meta", b"".join(children))


def video_track(compressor: str) -> bytes:
    stbl = container(b"stbl", stsd(visual_entry(b"avc1", compressor), audio_entry()))
    minf = container(b"minf", stbl)
    mdia = container(b"mdia", hdlr(b"vide", "VideoHandler"), minf)
    return container(b"trak", mdia)


def file(*, major: bytes = b"isom", compatible=(b"iso2", b"mp41"),
         compressor: str = "Lavc libx264", encoder: str = "Lavf", quicktime: bool = False) -> bytes:
    meta_builder = meta_quicktime if quicktime else meta_iso
    meta = meta_builder(hdlr(b"mdir", "ilst"), container(b"ilst", ilst_tag(b"\xa9too", encoder)))
    udta = container(b"udta", meta)
    moov = container(b"moov", video_track(compressor), udta)
    return ftyp(major, *compatible) + moov
