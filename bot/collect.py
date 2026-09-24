"""Шаг 1: собрать новости, отфильтровать, переписать и отправить на модерацию.

Запуск: python -m bot.collect
"""
import sys
from datetime import timedelta

from . import events, media, store
from .config import (MODERATOR_CHAT_ID, PIPELINE, SOURCES, DRY_RUN, require)
from .llm import ask_json, render, write_post
from .relevance import prefilter
from .sources import collect_all
from .tg_api import CAPTION_LIMIT, TEXT_LIMIT, keyboard, send_message, send_photo
from .util import esc, fit_html, iso, now_utc, similarity, visible_len


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


FOLLOWUP_HINT = (
    "Это продолжение истории, о которой канал уже писал: «{title}». "
    "Не пересказывай старое — сделай акцент на том, что изменилось. "
    "Первая строка должна ясно говорить, что это развитие событий."
)


def _moderation_card(item, data, post_text, media_kind="", compact=False) -> str:
    """Карточка для лички: шапка со служебной информацией + сам пост.

    compact — укороченная шапка в одну строку, чтобы пост с картинкой влез
    в лимит подписи Telegram.
    """
    follow = ""
    if item.followup_of:
        follow = f"🔁 продолжение: «{esc(item.followup_of[:70])}»\n"
    meta = f"<i>{esc(item.source_name)} · {data.get('score')}/10"
    if compact:
        return f"{meta}</i>\n{follow}{'─' * 12}\n{post_text}"
    matched = ", ".join(item.matched[:4]) or "—"
    head = (
        f"<b>{esc(data['title'])}</b>\n"
        f"{meta} · {MEDIA_LABEL.get(media_kind, '')} · {esc(matched)}</i>\n"
        f"{follow}{'─' * 18}\n"
    )
    return head + post_text


def _send_card(item, data, post_text, blob, media_kind):
    """Отправляет карточку модератору. Возвращает (сообщение, is_photo, media_kind).

    Картинка остаётся у поста, если влезает сам пост — служебная шапка ради этого
    сокращается. Раньше длинная шапка лишала картинки и пост в канале.
    """
    if blob and visible_len(post_text) <= CAPTION_LIMIT:
        for card in (_moderation_card(item, data, post_text, media_kind),
                     _moderation_card(item, data, post_text, media_kind, compact=True),
                     post_text):
            if visible_len(card) <= CAPTION_LIMIT:
                msg = send_photo(MODERATOR_CHAT_ID, blob, card,
                                 reply_markup=keyboard(item.id), silent=True)
                return msg, True, media_kind
    card = _moderation_card(item, data, post_text, media_kind)
    if blob:
        card += "\n\n<i>Пост длиннее лимита подписи — уйдёт текстом.</i>"
        media_kind = ""
    msg = send_message(MODERATOR_CHAT_ID, fit_html(card, TEXT_LIMIT - 200),
                       reply_markup=keyboard(item.id), silent=True)
    return msg, False, media_kind


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
    log(f"После дедупликации по словам: {len(items)}")

    memory = events.load(int(PIPELINE.get("event_memory_days", 10)))
    if PIPELINE.get("event_dedupe", True) and items:
        before = items
        items = events.pick(items, memory, ask_json,
                            followups=bool(PIPELINE.get("followups", True)), log=log)
        # отсеянные повторы запоминаем как виденные — в следующий раз они
        # отпадут ещё до модели и не будут стоить запроса
        kept_ids = {i.id for i in items}
        for dup in before:
            if dup.id not in kept_ids:
                seen["items"].append({"id": dup.id, "title": dup.title, "url": dup.url,
                                      "at": iso(now_utc()), "dup": True})
        log(f"После дедупликации по событиям: {len(items)}")

    max_per_run = int(limit or PIPELINE.get("max_per_run", 5))
    min_llm_score = float(PIPELINE.get("min_llm_score", 6))
    queued_ids = {q["id"] for q in queue["items"]}

    sent = 0
    skipped_low = 0
    for item in items:
        if sent >= max_per_run:
            break
        if item.id in queued_ids:
            continue

        hint = FOLLOWUP_HINT.format(title=item.followup_of) if item.followup_of else ""
        try:
            data = write_post(item, variant_hint=hint)
        except RuntimeError as exc:
            log(f"  ! {item.title[:60]} — {exc}")
            continue

        seen["items"].append(
            {"id": item.id, "title": item.title, "url": item.url, "at": iso(now_utc())}
        )

        if not data.get("publish") or float(data.get("score", 0)) < min_llm_score:
            skipped_low += 1
            log(f"  – пропуск ({data.get('score')}): {item.title[:60]} — {data.get('reason', '')[:80]}")
            continue

        post_text = render(data, item.url, item.source_name)

        image_url = media.pick_image_url(item)
        blob, media_kind = media.resolve(
            data["title"], image_url, item.url, item.source_name
        )
        if not media_kind:
            log(f"    без картинки: фид дал {image_url[:60]!r}")
        msg, as_photo, media_kind = _send_card(item, data, post_text, blob, media_kind)
        events.remember(memory, item.id, data["title"], post_text)
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
                "followup_of": item.followup_of,
            }
        )
        sent += 1
        log(f"  → в модерацию ({data.get('score')}): {data['title'][:70]}")

    keep = int(PIPELINE.get("seen_keep", 1500))
    seen["items"] = seen["items"][-keep:]

    if not DRY_RUN:
        store.save("seen.json", seen)
        store.save("queue.json", queue)
        events.save(memory)

    log(f"Готово. Отправлено на модерацию: {sent}. В очереди всего: {len(queue['items'])}")
    main.last_report = {
        "sent": sent,
        "candidates": len(items),
        "skipped_low": skipped_low,
    }
    return sent


if __name__ == "__main__":
    sys.exit(main())
