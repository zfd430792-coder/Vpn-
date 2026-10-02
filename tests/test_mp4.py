"""Разбор MP4 (кружки, видео). Сверено с exiftool на настоящем файле."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import mp4build as mb
from voicebot import mp4
from voicebot.handlers import mp4_report

FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)

def rejected(data):
    try:
        mp4.inspect(data)
        return False
    except mp4.NotMp4:
        return True


print("\n[1] Настоящий файл из ffmpeg — как у exiftool")
info = mp4.inspect((HERE / "note.mp4").read_bytes())
check("бренды", info.brands == ["isom", "iso2", "avc1", "mp41"], info.brands)
check("кодеки", info.codecs == ["avc1", "mp4a"], info.codecs)
check("имя кодировщика видео", info.tags.get("Compressor") == "Lavc60.31.102 libx264", info.tags)
check("©too", info.tags.get("©too") == "Lavf60.16.100", info.tags)
check("не пусто", not info.empty())

print("\n[2] Собранный вручную MP4")
info = mp4.inspect(mb.file(major=b"mp42", compatible=(b"isom", b"avc1"),
                           compressor="Lavc61 libx264", encoder="HandBrake 1.7"))
check("major-бренд первым", info.brands[0] == "mp42", info.brands)
check("совместимые бренды", "avc1" in info.brands, info.brands)
check("кодеки avc1 + mp4a", info.codecs == ["avc1", "mp4a"], info.codecs)
check("compressorname из видеозаписи", info.tags.get("Compressor") == "Lavc61 libx264", info.tags)
check("©too из ilst", info.tags.get("©too") == "HandBrake 1.7", info.tags)
check("обработчик виден", "VideoHandler" in info.handlers, info.handlers)

print("\n[3] meta в стиле QuickTime (без version/flags)")
info = mp4.inspect(mb.file(encoder="Apple iPhone", quicktime=True))
check("©too всё равно читается", info.tags.get("©too") == "Apple iPhone", info.tags)

print("\n[4] Юникод и экранирование")
info = mp4.inspect(mb.file(encoder="кодек ✓ <b>"))
check("UTF-8 в теге", info.tags.get("©too") == "кодек ✓ <b>", info.tags)

print("\n[5] Не MP4 и битые файлы")
check("пустой", rejected(b""))
check("Ogg вместо MP4", rejected(b"OggS" + b"\x00" * 60))
check("слишком короткий", rejected(b"\x00\x00\x00\x08ft"))
check("нет ftyp", rejected(b"\x00\x00\x00\x10moov" + b"\x00" * 8))
good = mb.file()
check("обрезанный не роняет парсер", mp4.inspect(good[:len(good) // 2]) is not None)
# размер бокса огромный — парсер не должен читать за концом
import struct
broken = bytearray(mb.file())
struct.pack_into(">I", broken, 0, 0x7FFFFFFF)
check("враньё про размер бокса не роняет", mp4.inspect(bytes(broken)) is not None)

print("\n[6] Текст ответа")
report = mp4_report(mp4.inspect(mb.file(compressor="Lavc libx264", encoder="Lavf")))
check("помечено как кружок/видео", "кружок или видео" in report, report[:60])
check("честно про модель", "Модель телефона по файлу тоже не узнать" in report)
check("показан кодировщик", "<code>Lavc libx264</code>" in report, report)
check("HTML из файла экранируется", "&lt;b&gt;" in mp4_report(mp4.inspect(mb.file(encoder="<b>"))))
empty = mp4_report(mp4.Mp4Info())
check("пустой разбор — без вранья", "Ничего опознаваемого" in empty, empty)

print()
if FAILS:
    print(f"❌ Провалено: {len(FAILS)}")
    sys.exit(1)
print("✅ Всё прошло")
