"""Предфильтр: дешёвая оценка «про Китай и про технологии?» до обращения к LLM."""
from .config import FILTERS


def _haystack(item) -> str:
    return f"{item.title} {item.summary}".lower()


def is_blocked(item) -> bool:
    text = _haystack(item)
    return any(bad.lower() in text for bad in FILTERS.get("blocklist") or [])


def about_china(item) -> bool:
    if item.china_native:
        return True
    text = _haystack(item)
    return any(m.lower() in text for m in FILTERS.get("china_markers") or [])


def score(item) -> tuple:
    """Возвращает (score, [что совпало])."""
    text = _haystack(item)
    total, matched = 0.0, []

    for name, w in (FILTERS.get("entities") or {}).items():
        if name.lower() in text:
            total += float(w)
            matched.append(name)
    for name, w in (FILTERS.get("topics") or {}).items():
        if name.lower() in text:
            total += float(w)
            matched.append(name)

    # заголовок весит больше, чем тело
    title = item.title.lower()
    for name in list(matched):
        if name.lower() in title:
            total += 0.5

    return round(total * item.weight, 2), matched[:8]


def prefilter(items: list, log=print) -> list:
    """Отсеивает мусор и проставляет score. Сортировка — по убыванию score."""
    min_score = float(FILTERS.get("min_score", 3.0))
    kept = []
    dropped = {"blocked": 0, "not_china": 0, "low_score": 0}

    for item in items:
        if is_blocked(item):
            dropped["blocked"] += 1
            continue
        if not about_china(item):
            dropped["not_china"] += 1
            continue
        item.score, item.matched = score(item)
        if item.score < min_score:
            dropped["low_score"] += 1
            continue
        kept.append(item)

    log(
        f"  предфильтр: оставлено {len(kept)}, "
        f"отброшено — стоп-слова {dropped['blocked']}, "
        f"не про Китай {dropped['not_china']}, низкий score {dropped['low_score']}"
    )
    return sorted(kept, key=lambda i: i.score, reverse=True)
