"""Шаг 1: собрать новости, отфильтровать, переписать и отправить на модерацию.

Запуск: python -m bot.collect
"""
import sys
from datetime import timedelta

from . import store
from .config import (MODERATOR_CHAT_ID, PIPELINE, SOURCES, DRY_RUN, require)
from .llm import render, write_post
from .relevance import prefilter
from .sources import collect_all
from .tg_api import keyboard, send_message
from .util import esc, iso, now_utc, similarity, truncate


def log(*args):
    print(*args, flush=True)


def _fresh(items, hours: int):
    cutoff = now_utc() - timedelta(hours=hours)
    out = []
    for item in items:
        # без даты не отбрасываем — многие фиды её не дают
        if item.published is None or item.published >= cutoff:
            out.append(item)
    return out


def _dedupe(items, seen_items, threshold: float):
    """Убирает и точные повторы по id/url, и разные заметки об одном событии."""
    seen_ids = {s["id"] for s in seen_items}
    seen_urls = {s.get("url") for s in seen_items if s.get("url")}
    seen_titles = [s["title"] for s in seen_items[-400:]]

    out = []
    for item in items:
        if item.id in seen_ids or (item.url and item.url in seen_urls):
            continue
        pool = seen_titles + [o.title for o in out]
        if any(similarity(item.title, t) >= threshold for t in pool):
            continue
        out.append(item)
    return out


def _moderation_card(item, data, post_text) -> str:
    matched = ", ".join(item.matched[:5]) or "—"
    head = (
        f"<b>{esc(data['title'])}</b>\n"
        f"<i>{esc(item.source_name)} · score {item.score} · оценка модели {data.get('score')}/10</i>\n"
        f"<i>{esc(matched)}</i>\n"
        f"{'─' * 18}\n"
    )
    return head + truncate(post_text, 3400)


def main() -> int:
    require("TELEGRAM_BOT_TOKEN", "MODERATOR_CHAT_ID", "DEEPSEEK_API_KEY")

    seen = store.load("seen.json")
    queue = store.load("queue.json")

    log("Сбор источников:")
    raw = collect_all(SOURCES, log=log)
    log(f"Всего собрано: {len(raw)}")

    items = _fresh(raw, int(PIPELINE.get("lookback_hours", 20)))
    log(f"Свежих за окно: {len(items)}")

    items = prefilter(items, log=log)
    items = items[: int(PIPELINE.get("max_candidates", 40))]

    items = _dedupe(items, seen["items"], float(PIPELINE.get("dedupe_similarity", 0.55)))
    log(f"После дедупликации: {len(items)}")

    max_per_run = int(PIPELINE.get("max_per_run", 5))
    min_llm_score = float(PIPELINE.get("min_llm_score", 6))
    queued_ids = {q["id"] for q in queue["items"]}

    sent = 0
    for item in items:
        if sent >= max_per_run:
            break
        if item.id in queued_ids:
            continue

        try:
            data = write_post(item)
        except RuntimeError as exc:
            log(f"  ! {item.title[:60]} — {exc}")
            continue

        seen["items"].append(
            {"id": item.id, "title": item.title, "url": item.url, "at": iso(now_utc())}
        )

        if not data.get("publish") or float(data.get("score", 0)) < min_llm_score:
            log(f"  – пропуск ({data.get('score')}): {item.title[:60]} — {data.get('reason', '')[:80]}")
            continue

        post_text = render(data, item.url, item.source_name)
        card = _moderation_card(item, data, post_text)

        msg = send_message(
            MODERATOR_CHAT_ID, card, reply_markup=keyboard(item.id), silent=True
        )
        queue["items"].append(
            {
                "id": item.id,
                "title": data["title"],
                "text": post_text,
                "url": item.url,
                "source_name": item.source_name,
                "source_id": item.source_id,
                "llm_score": data.get("score"),
                "raw_title": item.title,
                "raw_summary": item.summary,
                "lang": item.lang,
                "message_id": msg.get("message_id"),
                "created_at": iso(now_utc()),
                "rewrites": 0,
            }
        )
        sent += 1
        log(f"  → в модерацию ({data.get('score')}): {data['title'][:70]}")

    keep = int(PIPELINE.get("seen_keep", 1500))
    seen["items"] = seen["items"][-keep:]

    if not DRY_RUN:
        store.save("seen.json", seen)
        store.save("queue.json", queue)

    log(f"Готово. Отправлено на модерацию: {sent}. В очереди всего: {len(queue['items'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
