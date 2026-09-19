"""Разбор подписи к видео: название, сезон, серия, озвучка.

Поддерживаются три формы записи — админ пишет как удобно:

1. По строкам (регистр и язык ключей не важны):
       Название: Моё Аниме
       Сезон: 2
       Серия: 7
       Озвучка: Studio Band

2. Через разделитель:
       Моё Аниме | 2 | 7 | Studio Band
       Моё Аниме | 7 | Studio Band          (сезон 1)

3. Свободная строка с маркером серии:
       Моё Аниме S02E07 [Studio Band]
       Моё Аниме - 7 серия (Studio Band)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_DUB = "Основная"

_KEYS = {
    "title": {"название", "назва", "имя", "аниме", "тайтл", "name", "title", "anime"},
    "season": {"сезон", "season", "s"},
    "episode": {"серия", "эпизод", "серии", "episode", "ep", "e"},
    "dub": {"озвучка", "озвучки", "дубляж", "перевод", "студия", "dub", "voice", "audio"},
}

_SE = re.compile(r"\bs\s*(\d{1,2})\s*[\s._-]*e\s*(\d{1,4})\b", re.IGNORECASE)
_EP_WORD = re.compile(r"(\d{1,4})\s*(?:серия|серии|эпизод|ep\b|episode\b)", re.IGNORECASE)
_SEASON_WORD = re.compile(r"(?:сезон|season)\s*(\d{1,2})|(\d{1,2})\s*(?:сезон|season)", re.IGNORECASE)
_BRACKETS = re.compile(r"[\[(]([^\])]{2,40})[\])]")
_TRAILING_JUNK = re.compile(r"[\s\-–—_.,:;|]+$")


@dataclass
class Parsed:
    title: str
    season: int
    episode: int
    dub: str

    def __str__(self) -> str:
        return f"{self.title} · S{self.season} E{self.episode} · {self.dub}"


def _clean(value: str) -> str:
    return _TRAILING_JUNK.sub("", (value or "").strip()).strip()


def as_int(value: str, default: int = 0) -> int:
    """Вытаскивает число из строки. Публичная — ей пользуется админка."""
    digits = re.sub(r"\D", "", value or "")
    try:
        return int(digits)
    except ValueError:
        return default


_as_int = as_int  # совместимость


def _key_of(raw: str) -> Optional[str]:
    key = raw.strip().lower().rstrip(":").strip()
    for field, names in _KEYS.items():
        if key in names:
            return field
    return None


def _parse_keyvalue(text: str) -> Optional[Parsed]:
    found: dict[str, str] = {}
    loose_title = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        field = None
        if ":" in stripped:
            raw_key, _, raw_value = stripped.partition(":")
            field = _key_of(raw_key)
            if field and raw_value.strip():
                found.setdefault(field, raw_value.strip())
                continue
        if field is None and not loose_title:
            # строка без ключа — считаем её названием:
            # админ пишет имя первой строкой, а ниже сезон/серию/озвучку
            loose_title = stripped
    if "title" not in found and loose_title:
        found["title"] = loose_title
    if "title" not in found or "episode" not in found:
        return None
    return Parsed(
        title=_clean(found["title"]),
        season=_as_int(found.get("season", "1"), 1) or 1,
        episode=_as_int(found["episode"]),
        dub=_clean(found.get("dub", "")) or DEFAULT_DUB,
    )


def _parse_delimited(text: str) -> Optional[Parsed]:
    line = text.strip().splitlines()[0] if text.strip() else ""
    parts = [p.strip() for p in re.split(r"\s*[|/]\s*", line) if p.strip()]
    if len(parts) == 4:
        title, season, episode, dub = parts
        return Parsed(_clean(title), _as_int(season, 1) or 1, _as_int(episode), _clean(dub) or DEFAULT_DUB)
    if len(parts) == 3 and re.fullmatch(r"\D*\d+\D*", parts[1]):
        title, episode, dub = parts
        return Parsed(_clean(title), 1, _as_int(episode), _clean(dub) or DEFAULT_DUB)
    return None


def _parse_freeform(text: str) -> Optional[Parsed]:
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line:
        return None

    dub = ""
    match = _BRACKETS.search(line)
    if match:
        dub = match.group(1).strip()
        line = line[: match.start()] + line[match.end():]

    season, episode = 0, 0
    se = _SE.search(line)
    if se:
        season, episode = int(se.group(1)), int(se.group(2))
        line = line[: se.start()] + line[se.end():]
    else:
        ep = _EP_WORD.search(line)
        if not ep:
            return None
        episode = int(ep.group(1))
        line = line[: ep.start()] + line[ep.end():]
        sm = _SEASON_WORD.search(line)
        if sm:
            season = int(sm.group(1) or sm.group(2))
            line = line[: sm.start()] + line[sm.end():]

    title = _clean(line)
    if not title or not episode:
        return None
    return Parsed(title, season or 1, episode, dub or DEFAULT_DUB)


def parse(text: str) -> Optional[Parsed]:
    """Пытается разобрать подпись всеми способами. None — если не вышло."""
    if not text or not text.strip():
        return None
    for strategy in (_parse_keyvalue, _parse_delimited, _parse_freeform):
        result = strategy(text)
        if result and result.title and result.episode:
            return result
    return None
