"""Память о событиях: не присылать одну и ту же новость в пересказах разных изданий.

Старый дедуп сравнивал заголовки по словам. Он ловит перепечатки, но пропускает
одно событие у разных изданий, особенно на разных языках: «Alibaba unveils
Zhenwu V900» у TechNode и «阿里发布新一代自研AI芯片» у cnBeta для него — разные строки.

Здесь сравнение делает модель, одним запросом на всю подборку:
  1. группирует кандидатов подборки по событиям — из группы остаётся один;
  2. сверяет их с памятью — событиями, которые уже уходили модератору
     (одобренные, опубликованные и удалённые кнопкой — все);
  3. отличает повтор от продолжения истории: новые факты по старому событию
     не выкидываются, а приходят с пометкой «🔁 продолжение».

Если модель не ответила, подборка идёт как раньше — со старым дедупом по словам.
"""
from datetime import timedelta

from . import store
from .util import iso, now_utc, parse_iso, plain_text, truncate

FILE = "events.json"

SYSTEM = """Ты — выпускающий редактор новостного канала. Тебе дают два списка.

N — новые заметки из разных изданий, на разных языках (английский, китайский, русский).
M — события, которые редакция уже показывала (заголовки на русском).

Задача:
1. Раздели заметки N на группы: одна группа = одно событие. Одно событие — это когда
   пост по одной заметке сделает пост по другой повтором для читателя: тот же запуск,
   та же сделка, то же заявление, та же конференция с тем же анонсом — даже если
   издания пишут разными словами, на разных языках или с разными деталями.
   Разные события одной компании (например, дата презентации телефона и отдельная
   функция камеры) — разные группы. Если сомневаешься, считай одним событием.
2. Для каждой заметки проверь, не про то же ли событие, что уже есть в M.
3. Если заметка про событие из M, реши, есть ли в ней существенно новое развитие:
   начались продажи, объявили цену, вышли результаты, слух подтвердился официально.
   Другие формулировки, мелкие подробности и пересказ того же — не новое.

Ответ строго в JSON:
{"items": [{"n": 0, "group": "g1", "seen": "M3" или null, "new_facts": true/false}]}
Каждая заметка из N должна встретиться ровно один раз."""


# ── память ───────────────────────────────────────────────────────────────────

def _backfill(memory: dict) -> None:
    """Первый запуск: заполняем память тем, что уже есть в очередях и истории."""
    for name, date_key in (("published.json", "at"), ("approved.json", "created_at"),
                           ("queue.json", "created_at")):
        for entry in store.load(name).get("items", []):
            title = entry.get("title")
            if not title:
                continue
            memory["items"].append({
                "id": entry.get("id", ""),
                "title": title,
                "gist": truncate(plain_text(entry.get("text", "")), 160),
                "at": entry.get(date_key) or entry.get("at") or iso(now_utc()),
            })
    memory["backfilled"] = True


def load(days: int = 10, limit: int = 200) -> dict:
    memory = store.load(FILE)
    if not memory.get("backfilled"):
        _backfill(memory)
    cutoff = now_utc() - timedelta(days=days)
    memory["items"] = [
        e for e in memory["items"]
        if (parse_iso(e.get("at") or "") or now_utc()) >= cutoff
    ][-limit:]
    return memory


def remember(memory: dict, item_id: str, title: str, text: str) -> None:
    memory["items"].append({
        "id": item_id,
        "title": title,
        "gist": truncate(plain_text(text), 160),
        "at": iso(now_utc()),
    })


def save(memory: dict) -> None:
    store.save(FILE, memory)


# ── группировка ──────────────────────────────────────────────────────────────

def _user_prompt(items, memory_items) -> str:
    lines = ["N — новые заметки:"]
    for i, item in enumerate(items):
        summary = truncate(item.summary or "", 220)
        lines.append(f"N{i} [{item.source_name}] {item.title} — {summary}")
    lines.append("")
    lines.append("M — уже показанные события:")
    if not memory_items:
        lines.append("(пусто)")
    for j, entry in enumerate(memory_items):
        day = (entry.get("at") or "")[:10]
        gist = f" — {entry['gist']}" if entry.get("gist") else ""
        lines.append(f"M{j} ({day}) {entry['title']}{gist}")
    return "\n".join(lines)


def _index(value, prefix: str, size: int):
    """'M3' / 3 / '3' → 3; всё кривое → None."""
    if value is None:
        return None
    raw = str(value).strip().upper().lstrip(prefix)
    if not raw.isdigit():
        return None
    idx = int(raw)
    return idx if 0 <= idx < size else None


def pick(items: list, memory: dict, ask, followups: bool = True, log=print) -> list:
    """Оставляет по одной заметке на событие и выкидывает уже показанное.

    items должны быть отсортированы по убыванию приоритета — из группы остаётся
    первая. Заметкам-продолжениям проставляется item.followup_of = заголовок из памяти.
    """
    if not items:
        return items
    mem_items = memory.get("items", [])
    try:
        data = ask(SYSTEM, _user_prompt(items, mem_items))
    except RuntimeError as exc:
        log(f"  дедуп по событиям пропущен: {exc}")
        return items

    verdicts = {}
    for row in data.get("items") or []:
        if not isinstance(row, dict):
            continue
        n = _index(row.get("n"), "N", len(items))
        if n is None or n in verdicts:
            continue
        verdicts[n] = row

    groups, order = {}, []
    for n, item in enumerate(items):
        row = verdicts.get(n) or {}
        key = str(row.get("group") or f"_solo{n}")
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(n)

    kept, dup_in_batch, dup_seen, follow = [], 0, 0, 0
    for key in order:
        members = groups[key]
        dup_in_batch += len(members) - 1
        best = items[members[0]]

        seen_idx, new_facts = None, False
        for n in members:
            row = verdicts.get(n) or {}
            idx = _index(row.get("seen"), "M", len(mem_items))
            if idx is not None and seen_idx is None:
                seen_idx = idx
            new_facts = new_facts or bool(row.get("new_facts"))

        if seen_idx is None:
            kept.append(best)
        elif followups and new_facts:
            best.followup_of = mem_items[seen_idx]["title"]
            kept.append(best)
            follow += 1
        else:
            dup_seen += 1

    log(f"  события: групп {len(order)}, повторов внутри подборки {dup_in_batch}, "
        f"уже показано {dup_seen}, продолжений {follow}")
    return kept
