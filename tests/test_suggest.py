"""Сопоставление предложений: разные написания одного тайтла -> один голос."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anibot.search import normalize
from anibot.suggest import ASK, SURE, Candidate, acronym, match, score

FAILS = []
def check(label, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {label}{(' — ' + str(extra)) if extra and not cond else ''}")
    if not cond:
        FAILS.append(label)

def cand(cid, title, votes=0, aliases=()):
    return Candidate(cid, title, normalize(title), votes, [normalize(a) for a in aliases])

# трёхсловный тайтл — именно такой случай, где пишут и первым словом, и аббревиатурой
LONG = cand(1, "Клинок Рассекающий Демонов", votes=3)
OTHER = cand(2, "Магическая Битва", votes=1)
THIRD = cand(3, "Тихий Лес Осенью", votes=0)
POOL = [LONG, OTHER, THIRD]

print("\n[1] Аббревиатура из первых букв")
check("акроним строится верно", acronym(normalize("Клинок Рассекающий Демонов")) == "крд",
      acronym(normalize("Клинок Рассекающий Демонов")))
best, alts = match("крд", POOL)
check("«крд» уверенно попадает в тайтл", best is not None and best.candidate.id == 1,
      f"best={best and best.candidate.title} alts={[a.candidate.title for a in alts]}")
best, _ = match("КРД", POOL)
check("регистр не важен", best is not None and best.candidate.id == 1)
best, _ = match("мб", POOL)
check("«мб» попадает в двухсловный тайтл", best is not None and best.candidate.id == 2)

print("\n[2] Одно слово из названия")
best, _ = match("клинок", POOL)
check("«клинок» -> нужный тайтл", best is not None and best.candidate.id == 1)
best, _ = match("демонов", POOL)
check("слово не из начала тоже находится", best is not None and best.candidate.id == 1)

print("\n[3] Полное название и опечатки")
best, _ = match("Клинок Рассекающий Демонов", POOL)
check("полное название", best is not None and best.candidate.id == 1)
best, _ = match("клинок рассекающии демонов", POOL)
check("опечатка переживается", best is not None and best.candidate.id == 1)
best, _ = match("клинок, рассекающий демонов!", POOL)
check("пунктуация не мешает", best is not None and best.candidate.id == 1)

print("\n[4] Запомненные псевдонимы")
learned = cand(1, "Клинок Рассекающий Демонов", votes=3, aliases=["кими", "kimetsu"])
best, _ = match("кими", [learned, OTHER])
check("подтверждённый ранее псевдоним засчитывается сразу",
      best is not None and best.candidate.id == 1)
check("псевдоним даёт максимальную оценку", score("кими", learned) == 1.0,
      score("кими", learned))

print("\n[5] Чужое не приклеивается")
best, alts = match("совершенно другое аниме", POOL)
check("незнакомое -> новое предложение", best is None and not alts,
      f"best={best} alts={[a.candidate.title for a in alts]}")
best, alts = match("тихий лес осенью", POOL)
check("похожие слова не утаскивают в чужой тайтл",
      best is not None and best.candidate.id == 3)

print("\n[6] Спорное — спрашиваем, а не угадываем")
twins = [cand(10, "Лунный Клинок"), cand(11, "Лунный Клинов")]
best, alts = match("лунный клино", twins)
check("два почти одинаковых -> показываем выбор", best is None and len(alts) >= 2,
      f"best={best and best.candidate.title} alts={len(alts)}")
best, alts = match("магическ", POOL)
check("обрывок слова -> либо попадание, либо вопрос, но не молчаливый промах",
      (best is not None and best.candidate.id == 2) or any(a.candidate.id == 2 for a in alts),
      f"best={best and best.candidate.title} alts={[a.candidate.title for a in alts]}")

print("\n[7] Пороги")
check("порог уверенности выше порога вопроса", SURE > ASK, f"{SURE} vs {ASK}")
check("пустой запрос не матчится", score("", LONG) == 0.0)
check("один символ не утаскивает в тайтл", score("к", LONG) < SURE, score("к", LONG))

print("\n" + "─" * 46)
if FAILS:
    print(f"❌ ПРОВАЛЕНО: {len(FAILS)}")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ Сопоставление предложений работает")
