"""Шаг 3: публикация одобренных постов в канал по слотам.

Запуск: python -m bot.publish
Постим не чаще min_gap_minutes и только внутри окна window_start..window_end,
чтобы канал не выдавал пачку постов подряд и не писал ночью.
"""
import sys
from datetime import timedelta

from . import media, store
from .config import CHANNEL_ID, DRY_RUN, PUBLISHING, require
from .tg_api import CAPTION_LIMIT, send_message, send_photo
from .util import iso, local_now, now_utc, parse_iso, visible_len


def log(*args):
    print(*args, flush=True)


def publish_now(post: dict) -> dict:
    """Отправляет пост в канал — с картинкой, если она есть и подпись влезает."""
    blob = None
    if post.get("media_kind"):
        blob, _ = media.resolve(
            post.get("title", ""), post.get("image_url", ""),
            post.get("url", ""), post.get("source_name", ""),
        )
    if blob and visible_len(post["text"]) <= CAPTION_LIMIT:
        msg = send_photo(CHANNEL_ID, blob, post["text"], silent=False)
    else:
        msg = send_message(
            CHANNEL_ID, post["text"], preview_url=post.get("url"), silent=False
        )
    published = store.load("published.json")
    published["items"].append(
        {
            "id": post["id"],
            "title": post.get("title"),
            "url": post.get("url"),
            "message_id": msg.get("message_id"),
            "at": iso(now_utc()),
        }
    )
    published["items"] = published["items"][-500:]
    published["last_at"] = iso(now_utc())
    if not DRY_RUN:
        store.save("published.json", published)
    return msg


def in_window() -> bool:
    offset = int(PUBLISHING.get("timezone_offset", 3))
    hour = local_now(offset).hour
    return int(PUBLISHING.get("window_start", 9)) <= hour < int(PUBLISHING.get("window_end", 22))


def gap_ok(published: dict) -> bool:
    last = parse_iso(published.get("last_at") or "")
    if not last:
        return True
    gap = int(PUBLISHING.get("min_gap_minutes", 90))
    return now_utc() - last >= timedelta(minutes=gap)


def today_count(published: dict) -> int:
    offset = int(PUBLISHING.get("timezone_offset", 3))
    today = local_now(offset).date()
    count = 0
    for entry in published.get("items", []):
        at = parse_iso(entry.get("at") or "")
        if at and (at + timedelta(hours=offset)).date() == today:
            count += 1
    return count


def main() -> int:
    require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID")

    approved = store.load("approved.json")
    if not approved["items"]:
        log("Очередь на публикацию пуста.")
        return 0

    published = store.load("published.json")

    if not in_window():
        log("Вне окна публикации — ждём.")
        return 0
    if not gap_ok(published):
        log("Слишком рано после прошлого поста — ждём.")
        return 0
    if today_count(published) >= int(PUBLISHING.get("max_per_day", 6)):
        log("Дневной лимит постов исчерпан.")
        return 0

    post = approved["items"].pop(0)
    publish_now(post)
    if not DRY_RUN:
        store.save("approved.json", approved)
    log(f"Опубликовано: {post.get('title', '')[:70]}")
    log(f"Осталось в очереди: {len(approved['items'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
