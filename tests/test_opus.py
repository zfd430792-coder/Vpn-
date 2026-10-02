"""Разбор Vendor и таблица клиентов. Сеть не нужна."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import ogg
from voicebot import clients, opus
from voicebot.handlers import verdict

FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)

def vendor_of(data):
    try:
        return opus.read_vendor(data)
    except opus.NotOpus as e:
        return f"NotOpus: {e}"

def rejected(data):
    return vendor_of(data).startswith("NotOpus")


print("\n[1] Строки из таблицы читаются из файла")
for vendor in clients.KNOWN:
    got = vendor_of(ogg.voice(vendor))
    check(f"{vendor}", got == vendor, got)

print("\n[2] Настоящий файл из ffmpeg")
got = vendor_of((HERE / "ffmpeg.ogg").read_bytes())
check("Vendor = Lavf60.16.100, как у exiftool", got == "Lavf60.16.100", got)

print("\n[3] Заголовок на нескольких страницах")
long = "v" * 1000
got = vendor_of(ogg.voice(long, ("encoder=test", "x=" + "y" * 700)))
check("Vendor на 1000 байт", got == long, len(got))
got = vendor_of(ogg.voice(long, ("x=" + "y" * 700,), max_segments=1))
check("OpusTags разрезан на 8 страниц", got == long, len(got))
# пакет ровно 510 байт закрывается пустым сегментом — тот уезжает на следующую страницу
vendor = "z" * (510 - 16)
check("пакет кратен 255, пустой сегмент на новой странице",
      vendor_of(ogg.voice(vendor, max_segments=2)) == vendor)
check("UTF-8 в строке", vendor_of(ogg.voice("кодек ✓ 1.0")) == "кодек ✓ 1.0")
check("битый UTF-8 не роняет", vendor_of(ogg.voice(b"lib\xffopus")) == "lib�opus")
check("хвост после заголовков не мешает",
      vendor_of(ogg.voice("libopus 1.5.1") + b"\x00garbage") == "libopus 1.5.1")

print("\n[4] Чужой поток в том же файле")
other = ogg.pages([b"\x80theora" + b"\x00" * 30], serial=1, bos=True)
head = ogg.pages([ogg.opus_head()], serial=2, bos=True)
tags = ogg.pages([ogg.opus_tags("libopus unknown")], serial=2, seq=1)
foreign = ogg.pages([b"OpusTags-not-really"], serial=1, seq=1)
got = vendor_of(b"".join(other + head + foreign + tags))
check("берётся OpusTags своего потока", got == "libopus unknown", got)

print("\n[5] Не Opus и порченые файлы")
good = ogg.voice("libopus 1.5.1")
check("пустой файл", rejected(b""))
check("MP3", rejected(b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 100))
check("M4A", rejected(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 100))
vorbis = ogg.pages([b"\x01vorbis" + b"\x00" * 23], serial=3, bos=True)
vorbis += ogg.pages([b"\x03vorbis" + ogg.opus_tags("Xiph")[8:]], serial=3, seq=1)
check("Ogg, но Vorbis", rejected(b"".join(vorbis)))
check("обрезан на заголовке страницы", rejected(good[:47 + 10]))
check("обрезан посреди OpusTags", rejected(good[:47 + 40]))
check("за OpusHead не OpusTags",
      rejected(b"".join(ogg.pages([ogg.opus_head(), b"NotTags"], serial=4, bos=True))))
lying = ogg.opus_tags("abc")[:8] + (10_000).to_bytes(4, "little") + b"abc" + b"\x00" * 4
check("длина строки больше пакета", rejected(b"".join(
    ogg.pages([ogg.opus_head()], serial=5, bos=True) + ogg.pages([lying], serial=5, seq=1))))

print("\n[6] Какой клиент")
expect = {
    "libopus 1.5.1": "iPhone",
    "libopus unknown-fixed": "Android",
    "libopus unknown": "Telegram X",
    "libopus 1.3.1-fixed": "macOS (нативный клиент)",
    "Lavf60.16.101": "Telegram Desktop",
    "tweb": "Telegram Web K",
    "telegram-web-a": "Telegram Web A",
}
for vendor, client in expect.items():
    guess = clients.identify(vendor)
    check(f"{vendor} → {client}", guess == clients.Guess(client, exact=True), guess)
check("пробелы по краям не мешают", clients.identify(" libopus 1.5.1\n") == clients.Guess("iPhone", True))
for vendor, client in [("Lavf61.7.100", "Telegram Desktop"), ("tweb 2.2", "Telegram Web K"),
                       ("telegram-web-a 10.9.0", "Telegram Web A")]:
    check(f"{vendor} → похоже на {client}", clients.identify(vendor) == clients.Guess(client, False))
for vendor in ["libopus 1.5.2", "libopus", "Recorder", ""]:
    check(f"{vendor!r} не угадываем", clients.identify(vendor) is None, clients.identify(vendor))

print("\n[7] Текст ответа")
text = verdict("libopus 1.5.1")
check("точное совпадение", text.startswith("✅ <b>iPhone</b>") and "<code>libopus 1.5.1</code>" in text, text)
check("совпало семейство", verdict("Lavf61.7.100").startswith("🤔 Похоже на <b>Telegram Desktop</b>"))
check("неизвестный", verdict("Recorder").startswith("🤷"))
check("HTML из файла экранируется", "<code>&lt;b&gt;&amp;</code>" in verdict("<b>&"), verdict("<b>&"))
check("длинная строка обрезается", len(verdict("q" * 5000)) < 400)
check("непечатаемое заменяется", "<code>lib�opus�</code>" in verdict("lib\x00opus\n"), verdict("lib\x00opus\n"))
check("пустая строка", "пустая строка" in verdict(""))

print()
if FAILS:
    print(f"❌ Провалено: {len(FAILS)}")
    sys.exit(1)
print("✅ Всё прошло")
