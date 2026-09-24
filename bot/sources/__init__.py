"""Сбор сырых новостей из всех типов источников."""
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class Item:
    id: str
    title: str
    summary: str
    url: str
    source_id: str
    source_name: str
    lang: str = "en"
    china_native: bool = False
    weight: float = 1.0
    published: Optional[datetime] = None
    image: str = ""
    score: float = 0.0
    matched: list = field(default_factory=list)
    followup_of: str = ""       # заголовок уже показанного события, если это продолжение

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        return d


from .rss import fetch_rss          # noqa: E402
from .tg import fetch_telegram      # noqa: E402
from .hn import fetch_hackernews    # noqa: E402


def collect_all(cfg: dict, log=print) -> list:
    """Опрашивает все источники параллельно: с двумя десятками лент по очереди
    кнопка «🔎 5 новостей» отвечала бы минуту и дольше."""
    from concurrent.futures import ThreadPoolExecutor

    jobs = []   # (подпись в логе, функция, конфиг)
    for src in cfg.get("rss") or []:
        if src.get("enabled", True):
            jobs.append((src["name"], fetch_rss, src))
    for src in cfg.get("telegram") or []:
        if src.get("enabled", True):
            jobs.append((f"tg/{src['username']}", fetch_telegram, src))
    hn = cfg.get("hackernews") or {}
    if hn.get("enabled"):
        jobs.append(("HackerNews", fetch_hackernews, hn))

    def run(job):
        label, fn, src = job
        try:
            return label, fn(src), None
        except Exception as exc:  # источник упал — остальные не роняем
            return label, [], exc

    items: list = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for label, got, exc in pool.map(run, jobs):   # map сохраняет порядок
            if exc is not None:
                log(f"  {label:<16} ошибка: {str(exc)[:120]}")
            else:
                items.extend(got)
                log(f"  {label:<16} {len(got):>3} шт.")
    return items
