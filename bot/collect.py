"""Шаг 1: собрать новости, отфильтровать, переписать и отправить на модерацию.

Запуск: python -m bot.collect
"""
import sys
from datetime import timedelta

from . import media, store
from .config import (MODERATOR_CHAT_ID, PIPELINE, SOURCES, DRY_RUN, require)
from .llm import render, write_post
from .relevance import prefilter
from .sources import collect_all
from .tg_api import CAPTION_LIMIT, keyboard, send_message, send_photo
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


MEDIA_LABEL = {"source": "фото источника", "card": "своя карточка", "": "без картинки"}


def _moderation_card(item, data, post_text, media_kind="") -> str:
    matched = ", ".join(item.matched[:4]) or "—"
    head = (
        f"<b>{esc(data['title'])}</b>\n"
        f"<i>{esc(item.source_name)} · {data.get('score')}/10 · "
        f"{MEDIA_LABEL.get(media_kind, '')} · {esc(matched)}</i>\n"
        f"{'─' * 18}\n"
    )
    return head + post_text


def main(limit: int = None, force: bool = False) -> int:
    require("TELEGRAM_BOT_TOKEN", "MODERATOR_CHAT_ID", "DEEPSEEK_API_KEY")

    seen = store.load("seen.json")
    queue = store.load("queue.json")

    # не заваливаем личку: пока не разобрана накопленная очередь, новое не собираем
    max_queue = int(PIPELINE.get("max_queue", 6))
    if not force and len(queue["items"]) >= max_queue:
        log(f"В очереди на модерации {len(queue['items'])} — это предел ({max_queue}), "
            f"сбор пропущен. Разберите карточки, и сбор возобновится сам.")
        return 0

    log("Сбор источников:")
    raw = collect_all(SOURCES, log=log)
    log(f"Всего собрано: {len(raw)}")

    items = _fresh(raw, int(PIPELINE.get("lookback_hours", 20)))
    log(f"Свежих за окно: {len(items)}")

    items = prefilter(items, log=log)
    items = items[: int(PIPELINE.get("max_candidates", 40))]

    items = _dedupe(items, seen["items"], float(PIPELINE.get("dedupe_similarity", 0.55)))
    log(f"После дедупликации: {len(items)}")

    max_per_run = int(limit or PIPELINE.get("max_per_run", 5))
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

        image_url = media.pick_image_url(item)
        blob, media_kind = media.resolve(
            data["title"], image_url, item.url, item.source_name
        )
        if not media_kind:
            log(f"    без картинки: фид дал {image_url[:60]!r}")
        card = _moderation_card(item, data, post_text, media_kind)

        as_photo = bool(blob) and len(card) <= CAPTION_LIMIT
        if as_photo:
            msg = send_photo(MODERATOR_CHAT_ID, blob, card,
                             reply_markup=keyboard(item.id), silent=True)
        else:
            if blob:
                card += "\n\n<i>Пост длиннее лимита подписи — уйдёт текстом.</i>"
                media_kind = ""
            msg = send_message(MODERATOR_CHAT_ID, truncate(card, 3400),
                               reply_markup=keyboard(item.id), silent=True)
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
                "image_url": image_url,
                "media_kind": media_kind,
                "mod_is_photo": as_photo,
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
    return sent


if __name__ == "__main__":
    sys.exit(main())
