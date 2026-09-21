"""Шаг 2: обработка нажатий кнопок в личке модератора.

Запуск: python -m bot.moderate
Забирает накопившиеся апдейты через getUpdates и применяет решения.
"""
import sys
from datetime import timedelta

from . import store
from .config import DRY_RUN, require
from .llm import render, write_post
from .publish import publish_now
from .tg_api import answer_callback, edit_message, get_updates, keyboard, send_message
from .util import esc, now_utc, parse_iso, truncate

REWRITE_HINT = (
    "Предыдущий вариант поста не подошёл. Напиши заново: другой заголовок, "
    "другой угол подачи, другая первая строка. Факты те же."
)
MAX_REWRITES = 3


def log(*args):
    print(*args, flush=True)


class _Item:
    """Минимальный объект новости — восстанавливаем из очереди для повторного рерайта."""

    def __init__(self, entry):
        self.title = entry.get("raw_title") or entry.get("title", "")
        self.summary = entry.get("raw_summary", "")
        self.source_name = entry.get("source_name", "")
        self.lang = entry.get("lang", "en")
        self.published = None
        self.url = entry.get("url", "")


def _find(queue, item_id):
    for entry in queue["items"]:
        if entry["id"] == item_id:
            return entry
    return None


def _drop(queue, item_id):
    queue["items"] = [e for e in queue["items"] if e["id"] != item_id]


def _card_footer(entry, status):
    return (
        f"<b>{esc(entry.get('title', ''))}</b>\n"
        f"<i>{esc(entry.get('source_name', ''))} · {status}</i>\n"
        f"{'─' * 18}\n" + truncate(entry.get("text", ""), 3400)
    )


def handle_callback(cb, queue, approved):
    data = cb.get("data", "")
    action, _, item_id = data.partition(":")
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    entry = _find(queue, item_id)
    if entry is None:
        answer_callback(cb["id"], "Эта новость уже обработана")
        return

    if action == "q":
        _drop(queue, item_id)
        approved["items"].append(entry)
        answer_callback(cb["id"], "В очереди на публикацию")
        edit_message(chat_id, message_id, _card_footer(entry, "✅ в очереди"), reply_markup={"inline_keyboard": []})
        log(f"  ✅ в очередь: {entry.get('title', '')[:60]}")

    elif action == "n":
        _drop(queue, item_id)
        try:
            publish_now(entry)
        except RuntimeError as exc:
            answer_callback(cb["id"], "Ошибка публикации")
            edit_message(chat_id, message_id, _card_footer(entry, f"⚠️ ошибка: {esc(str(exc))[:80]}"))
            return
        answer_callback(cb["id"], "Опубликовано")
        edit_message(chat_id, message_id, _card_footer(entry, "⚡ опубликовано"), reply_markup={"inline_keyboard": []})
        log(f"  ⚡ опубликовано: {entry.get('title', '')[:60]}")

    elif action == "r":
        if entry.get("rewrites", 0) >= MAX_REWRITES:
            answer_callback(cb["id"], "Лимит переписываний исчерпан")
            return
        answer_callback(cb["id"], "Переписываю…")
        try:
            fresh = write_post(_Item(entry), variant_hint=REWRITE_HINT, temperature=0.9)
        except RuntimeError as exc:
            edit_message(chat_id, message_id, _card_footer(entry, f"⚠️ {esc(str(exc))[:80]}"),
                         reply_markup=keyboard(item_id))
            return
        entry["text"] = render(fresh, entry.get("url", ""), entry.get("source_name", ""))
        entry["title"] = fresh.get("title", entry.get("title", ""))
        entry["llm_score"] = fresh.get("score")
        entry["rewrites"] = entry.get("rewrites", 0) + 1
        status = f"вариант {entry['rewrites'] + 1} · оценка {fresh.get('score')}/10"
        edit_message(chat_id, message_id, _card_footer(entry, status), reply_markup=keyboard(item_id))
        log(f"  ♻️ переписано: {entry.get('title', '')[:60]}")

    elif action == "d":
        _drop(queue, item_id)
        answer_callback(cb["id"], "Удалено")
        edit_message(chat_id, message_id, _card_footer(entry, "🗑 удалено"), reply_markup={"inline_keyboard": []})
        log(f"  🗑 удалено: {entry.get('title', '')[:60]}")

    else:
        answer_callback(cb["id"], "Неизвестная команда")


def handle_message(msg, queue, approved):
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if not text.startswith("/"):
        return

    if text.startswith("/start") or text.startswith("/id"):
        send_message(chat_id, f"Ваш chat_id: <code>{chat_id}</code>\n"
                              f"Впишите его в секрет <code>MODERATOR_CHAT_ID</code>.")
    elif text.startswith("/queue"):
        published = store.load("published.json")
        send_message(
            chat_id,
            f"На модерации: <b>{len(queue['items'])}</b>\n"
            f"Одобрено, ждёт слота: <b>{len(approved['items'])}</b>\n"
            f"Опубликовано всего: <b>{len(published['items'])}</b>\n"
            f"Последний пост: {esc(published.get('last_at') or '—')}",
        )
    elif text.startswith("/next"):
        if not approved["items"]:
            send_message(chat_id, "Очередь публикации пуста.")
            return
        post = approved["items"].pop(0)
        publish_now(post)
        send_message(chat_id, f"Опубликовано: {esc(post.get('title', ''))}")
    elif text.startswith("/help"):
        send_message(
            chat_id,
            "/queue — что в очередях\n"
            "/next — опубликовать следующий одобренный пост прямо сейчас\n"
            "/id — показать chat_id",
        )


def main() -> int:
    require("TELEGRAM_BOT_TOKEN")

    offset_state = store.load("offset.json")
    queue = store.load("queue.json")
    approved = store.load("approved.json")

    updates = get_updates(offset_state["offset"])
    if not updates:
        log("Новых апдейтов нет.")
        return 0

    log(f"Апдейтов: {len(updates)}")
    for update in updates:
        offset_state["offset"] = update["update_id"] + 1
        try:
            if "callback_query" in update:
                handle_callback(update["callback_query"], queue, approved)
            elif "message" in update:
                handle_message(update["message"], queue, approved)
        except Exception as exc:  # один битый апдейт не должен ронять прогон
            log(f"  ! ошибка обработки: {exc}")

    # подчищаем протухшие карточки: неотвеченные дольше 3 суток
    cutoff = now_utc() - timedelta(days=3)
    before = len(queue["items"])
    queue["items"] = [
        e for e in queue["items"]
        if (parse_iso(e.get("created_at") or "") or now_utc()) >= cutoff
    ]
    if before != len(queue["items"]):
        log(f"  протухло и удалено из очереди: {before - len(queue['items'])}")

    if not DRY_RUN:
        store.save("offset.json", offset_state)
        store.save("queue.json", queue)
        store.save("approved.json", approved)
    log("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
