"""Проверка живости всех источников: python scripts/check_sources.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import SOURCES                      # noqa: E402
from bot.sources import fetch_hackernews, fetch_rss, fetch_telegram  # noqa: E402

def main():
    bad = 0
    for src in SOURCES.get("rss") or []:
        mark = " " if src.get("enabled", True) else "·"
        try:
            items = fetch_rss(src)
            newest = max((i.published for i in items if i.published), default=None)
            print(f"{mark} OK   {src['name']:<16} {len(items):>3} шт.  свежее: {newest}")
            if not items:
                bad += 1
        except Exception as exc:
            print(f"{mark} FAIL {src['name']:<16} {exc}")
            bad += 1
    for src in SOURCES.get("telegram") or []:
        if not src.get("enabled", True):
            continue
        try:
            items = fetch_telegram(src)
            print(f"  OK   tg/{src['username']:<13} {len(items):>3} шт.")
        except Exception as exc:
            print(f"  FAIL tg/{src['username']:<13} {exc}")
            bad += 1
    hn = SOURCES.get("hackernews") or {}
    if hn.get("enabled"):
        try:
            items = fetch_hackernews(hn)
            print(f"  OK   {'HackerNews':<16} {len(items):>3} шт.")
        except Exception as exc:
            print(f"  FAIL {'HackerNews':<16} {exc}")
            bad += 1
    print(f"\nПроблемных источников: {bad}")
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())
