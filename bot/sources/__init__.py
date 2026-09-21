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
    score: float = 0.0
    matched: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["published"] = self.published.isoformat() if self.published else None
        return d


from .rss import fetch_rss          # noqa: E402
from .tg import fetch_telegram      # noqa: E402
from .hn import fetch_hackernews    # noqa: E402


def collect_all(cfg: dict, log=print) -> list:
    items: list = []
    for src in cfg.get("rss") or []:
        if not src.get("enabled", True):
            continue
        try:
            got = fetch_rss(src)
            items.extend(got)
            log(f"  {src['name']:<16} {len(got):>3} шт.")
        except Exception as exc:  # источник упал — остальные не роняем
            log(f"  {src['name']:<16} ошибка: {exc}")
    for src in cfg.get("telegram") or []:
        if not src.get("enabled", True):
            continue
        try:
            got = fetch_telegram(src)
            items.extend(got)
            log(f"  tg/{src['username']:<12} {len(got):>3} шт.")
        except Exception as exc:
            log(f"  tg/{src['username']:<12} ошибка: {exc}")
    hn = cfg.get("hackernews") or {}
    if hn.get("enabled"):
        try:
            got = fetch_hackernews(hn)
            items.extend(got)
            log(f"  {'HackerNews':<16} {len(got):>3} шт.")
        except Exception as exc:
            log(f"  {'HackerNews':<16} ошибка: {exc}")
    return items
