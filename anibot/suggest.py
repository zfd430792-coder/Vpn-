"""Сопоставление предложений.

Люди пишут одно и то же по-разному: полным названием, одним словом,
сокращением из первых букв. Чтобы голоса не растекались по десятку
почти одинаковых строк, входящий текст сравнивается с уже собранными
предложениями и с их запомненными псевдонимами.

Логика намеренно осторожная:

* уверенное совпадение (>= SURE) засчитывается молча;
* сомнительное (>= ASK) — бот покажет варианты и спросит, что имелось в виду;
* ничего похожего — заводится новое предложение.

Подтверждённый человеком вариант сохраняется псевдонимом, поэтому в
следующий раз то же сокращение попадёт в цель без вопросов — система
дообучается на ответах людей, а не угадывает с нуля.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from .search import normalize

SURE = 0.85   # засчитываем молча
ASK = 0.62    # спрашиваем, что имелось в виду


@dataclass
class Candidate:
    id: int
    title: str
    norm: str
    votes: int = 0
    aliases: list[str] = field(default_factory=list)


@dataclass
class Match:
    candidate: Candidate
    score: float


def acronym(norm: str) -> str:
    """«клинок рассекающий демонов» -> «крд»"""
    return "".join(word[0] for word in norm.split() if word)


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def score(query: str, cand: Candidate) -> float:
    """Насколько запрос похож на кандидата. 0 — совсем не похож."""
    q = normalize(query)
    if not q:
        return 0.0

    aliases = [a for a in cand.aliases if a]
    if q == cand.norm or q in aliases:
        return 1.0

    # сокращение по первым буквам
    squashed = q.replace(" ", "")
    if 2 <= len(squashed) <= 6:
        if squashed == acronym(cand.norm):
            return 0.96
        for alias in aliases:
            if squashed == acronym(alias):
                return 0.94

    words = cand.norm.split()

    # назвали одним словом из названия — чаще всего первым
    if len(words) > 1 and len(q) >= 4:
        if q == words[0]:
            return 0.92
        if q in words:
            return 0.88

    if len(q) >= 4 and cand.norm.startswith(q):
        return 0.90

    if len(q) >= 5 and q in cand.norm:
        return 0.86

    # все слова запроса встречаются в названии
    query_words = set(q.split())
    if query_words and query_words <= set(words):
        return 0.84

    best = _ratio(q, cand.norm)
    for alias in aliases:
        best = max(best, _ratio(q, alias) * 0.98)
    return best if best >= ASK else 0.0


def match(query: str, candidates: list[Candidate]) -> tuple[Match | None, list[Match]]:
    """Возвращает (уверенное совпадение, список спорных вариантов).

    Если уверенное найдено — второй список пуст. Если нет, во втором
    лежат похожие кандидаты, которые стоит показать человеку.
    """
    scored = [Match(c, score(query, c)) for c in candidates]
    scored = [m for m in scored if m.score >= ASK]
    scored.sort(key=lambda m: (-m.score, -m.candidate.votes, m.candidate.id))

    if not scored:
        return None, []

    best = scored[0]
    if best.score >= SURE:
        # два одинаково уверенных варианта — лучше переспросить, чем слить не то
        runner_up = scored[1] if len(scored) > 1 else None
        if runner_up is not None and best.score - runner_up.score < 0.03:
            return None, scored[:4]
        return best, []

    return None, scored[:4]
