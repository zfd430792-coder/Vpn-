"""Нормализация названий и нечёткий поиск."""

from __future__ import annotations

import difflib
import re

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Приводит название к ключу поиска: регистр, ё/е, пунктуация, пробелы."""
    text = (text or "").lower().replace("ё", "е").replace("_", " ")
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def rank(query: str, candidates: list[tuple[int, str]]) -> list[int]:
    """Сортирует кандидатов (id, норм. название) по близости к запросу.

    Точное совпадение и префикс идут первыми, дальше — подстрока,
    в конце — нечёткое совпадение по difflib.
    """
    q = normalize(query)
    if not q:
        return []
    scored: list[tuple[float, int, int]] = []
    for cid, norm in candidates:
        if norm == q:
            score = 1.0
        elif norm.startswith(q):
            score = 0.9
        elif q in norm:
            score = 0.8
        else:
            score = difflib.SequenceMatcher(None, q, norm).ratio()
            if score < 0.5:
                continue
        scored.append((score, -len(norm), cid))
    scored.sort(reverse=True)
    return [cid for _, _, cid in scored]
