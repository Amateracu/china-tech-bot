"""Состояние в JSON-файлах внутри репозитория (их коммитит workflow)."""
import json
from pathlib import Path

from .config import STATE_DIR

DEFAULTS = {
    "seen.json": {"items": []},          # [{id, title, url, at}] — для дедупа
    "queue.json": {"items": []},         # посты, ждущие решения модератора
    "approved.json": {"items": []},      # одобренные, ждут слота публикации
    "published.json": {"items": [], "last_at": None},
    "offset.json": {"offset": 0},        # offset для getUpdates
}


def path(name: str) -> Path:
    return STATE_DIR / name


def load(name: str) -> dict:
    p = path(name)
    if not p.exists():
        return json.loads(json.dumps(DEFAULTS[name]))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(DEFAULTS[name]))
    base = json.loads(json.dumps(DEFAULTS[name]))
    base.update(data if isinstance(data, dict) else {})
    return base


def save(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path(name).write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )
