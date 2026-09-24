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
from .tg_api import (CAPTION_LIMIT, answer_callback, edit_caption, edit_message,
                     get_updates, keyboard, send_message)
from .util import esc, fit_html, now_utc, parse_iso, sanitize_html, visible_len

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
        self.followup_of = entry.get("followup_of", "")


def _find(queue, item_id):
    for entry in queue["items"]:
        if entry["id"] == item_id:
            return entry
    return None


def _drop(queue, item_id):
    queue["items"] = [e for e in queue["items"] if e["id"] != item_id]


def _card_footer(entry, status):
    limit = CAPTION_LIMIT if entry.get("mod_is_photo") else 3400
    text = entry.get("text", "")
    full = (
        f"<b>{esc(entry.get('title', ''))}</b>\n"
        f"<i>{esc(entry.get('source_name', ''))} · {status}</i>\n"
        f"{'─' * 18}\n" + text
    )
    if visible_len(full) <= limit:
        return full
    short = f"<i>{status}</i>\n{'─' * 12}\n" + text
    if visible_len(short) <= limit:
        return short
    return fit_html(short, limit)


def _ack(callback_id, text=""):
    """Всплывающий ответ на нажатие — необязательный.

    Telegram принимает ответ на callback только первые ~15 минут. Мы просыпаемся
    по расписанию и часто опаздываем, поэтому неудача здесь не должна мешать
    применить само решение.
    """
    try:
        answer_callback(callback_id, text)
    except RuntimeError as exc:
        log(f"  (ответить на нажатие не вышло: {str(exc)[:70]})")


def _edit(entry, chat_id, message_id, text, reply_markup=None):
    """У сообщения с фото правится подпись, у обычного — текст. Тоже необязательно."""
    try:
        if entry.get("mod_is_photo"):
            return edit_caption(chat_id, message_id, text, reply_markup=reply_markup)
        return edit_message(chat_id, message_id, text, reply_markup=reply_markup)
    except RuntimeError as exc:
        log(f"  (обновить карточку не вышло: {str(exc)[:70]})")
        return None


def handle_callback(cb, queue, approved):
    data = cb.get("data", "")
    action, _, item_id = data.partition(":")
    message = cb.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")

    log(f"  нажатие: {data!r} от chat_id={chat_id}")

    entry = _find(queue, item_id)
    if entry is None:
        known = [e["id"] for e in queue["items"]]
        log(f"  ! новости {item_id!r} нет в очереди; в очереди {len(known)}: {known}")
        _ack(cb["id"], "Эта новость уже обработана")
        return

    if action == "q":
        _drop(queue, item_id)
        approved["items"].append(entry)
        _ack(cb["id"], "В очереди на публикацию")
        _edit(entry, chat_id, message_id, _card_footer(entry, "✅ в очереди"),
              reply_markup={"inline_keyboard": []})
        log(f"  ✅ в очередь: {entry.get('title', '')[:60]}")

    elif action == "n":
        try:
            publish_now(entry)
        except RuntimeError as exc:
            # новость остаётся в очереди: неудачная публикация не должна её терять
            log(f"  ! публикация не удалась: {exc}")
            _ack(cb["id"], "Ошибка публикации")
            _edit(entry, chat_id, message_id,
                  _card_footer(entry, f"⚠️ ошибка: {esc(str(exc))[:160]}"),
                  reply_markup=keyboard(item_id))
            return
        _drop(queue, item_id)
        _ack(cb["id"], "Опубликовано")
        _edit(entry, chat_id, message_id, _card_footer(entry, "⚡ опубликовано"),
              reply_markup={"inline_keyboard": []})
        log(f"  ⚡ опубликовано: {entry.get('title', '')[:60]}")

    elif action == "r":
        if entry.get("rewrites", 0) >= MAX_REWRITES:
            _ack(cb["id"], "Лимит переписываний исчерпан")
            return
        _ack(cb["id"], "Переписываю…")
        hint = REWRITE_HINT
        if entry.get("followup_of"):
            from .collect import FOLLOWUP_HINT
            hint += " " + FOLLOWUP_HINT.format(title=entry["followup_of"])
        try:
            fresh = write_post(_Item(entry), variant_hint=hint, temperature=0.9)
        except RuntimeError as exc:
            _edit(entry, chat_id, message_id,
                  _card_footer(entry, f"⚠️ {esc(str(exc))[:60]}"),
                  reply_markup=keyboard(item_id))
            return
        entry["text"] = render(fresh, entry.get("url", ""), entry.get("source_name", ""))
        entry["title"] = fresh.get("title", entry.get("title", ""))
        entry["llm_score"] = fresh.get("score")
        entry["rewrites"] = entry.get("rewrites", 0) + 1
        status = f"вариант {entry['rewrites'] + 1} · оценка {fresh.get('score')}/10"
        _edit(entry, chat_id, message_id, _card_footer(entry, status),
              reply_markup=keyboard(item_id))
        log(f"  ♻️ переписано: {entry.get('title', '')[:60]}")

    elif action == "e":
        _ack(cb["id"], "Жду новый текст")
        start_edit(entry, chat_id, message_id)

    elif action == "d":
        _drop(queue, item_id)
        _ack(cb["id"], "Удалено")
        _edit(entry, chat_id, message_id, _card_footer(entry, "🗑 удалено"),
              reply_markup={"inline_keyboard": []})
        log(f"  🗑 удалено: {entry.get('title', '')[:60]}")

    else:
        _ack(cb["id"], "Неизвестная команда")


SOURCE_MARK = '\n\n<a href="'
EDIT_TTL_MINUTES = 30


def split_body(text: str):
    """Делит пост на тело и строку источника — правим только тело."""
    if SOURCE_MARK in text:
        body, _, tail = text.partition(SOURCE_MARK)
        return body, SOURCE_MARK + tail
    return text, ""


def sanitize(text: str) -> str:
    """Экранируем всё, кроме тегов оформления Telegram — иначе пост отвергнут."""
    return sanitize_html(text)


def start_edit(entry, chat_id, message_id):
    """Запоминаем, какую новость правим, и отдаём текущий текст для копирования."""
    body, _ = split_body(entry.get("text", ""))
    store.save("edit.json", {"awaiting": {
        "item_id": entry["id"],
        "chat_id": chat_id,
        "card_message_id": message_id,
        "at": now_utc().isoformat(),
    }})
    send_message(chat_id,
                 "✏️ Пришлите новый текст поста следующим сообщением.\n"
                 "Текущий — ниже, его удобно переслать себе и поправить.\n"
                 "Отмена — <code>/cancel</code>.")
    send_message(chat_id, body)


def apply_edit(text, chat_id, queue):
    """Пришёл новый текст: подставляем его в карточку. True, если правка применена."""
    state = store.load("edit.json")
    awaiting = state.get("awaiting")
    if not awaiting or awaiting.get("chat_id") != chat_id:
        return False

    started = parse_iso(awaiting.get("at") or "")
    if started and now_utc() - started > timedelta(minutes=EDIT_TTL_MINUTES):
        store.save("edit.json", {"awaiting": None})
        send_message(chat_id, "Правка отменена: прошло слишком много времени.",
                     reply_markup=MENU)
        return True

    entry = _find(queue, awaiting["item_id"])
    store.save("edit.json", {"awaiting": None})
    if entry is None:
        send_message(chat_id, "Эта новость уже обработана — правка не применена.",
                     reply_markup=MENU)
        return True

    _, tail = split_body(entry.get("text", ""))
    entry["text"] = sanitize(text.strip()) + tail
    entry["edited"] = True
    _edit(entry, chat_id, entry.get("message_id"),
          _card_footer(entry, "✏️ поправлено вручную"),
          reply_markup=keyboard(entry["id"]))
    send_message(chat_id, "Готово, карточка обновлена.", reply_markup=MENU)
    log(f"  ✏️ правка вручную: {entry.get('title', '')[:60]}")
    return True



MENU = {
    "keyboard": [
        [{"text": "🔎 5 новостей"}],
        [{"text": "📋 Очередь"}, {"text": "⏭ Опубликовать"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

NEWS_WORDS = ("/news", "🔎 5 новостей", "новости", "/новости")
QUEUE_WORDS = ("/queue", "📋 Очередь")
NEXT_WORDS = ("/next", "⏭ Опубликовать")


def _fetch_news(chat_id, queue, how_many=5):
    """Сходить за новостями прямо сейчас, по нажатию кнопки.

    collect пишет очередь на диск сам, поэтому после него обязательно
    перечитываем её в тот же объект — иначе воркер затрёт свежие карточки
    своей устаревшей копией.
    """
    send_message(chat_id, "Ищу новости… это займёт полминуты.", reply_markup=MENU)
    try:
        from .collect import main as collect_main
        sent = collect_main(limit=how_many, force=True)
        queue["items"] = store.load("queue.json")["items"]
    except Exception as exc:
        log(f"  ! сбор по кнопке упал: {exc}")
        send_message(chat_id, f"Не получилось: {esc(str(exc))[:200]}", reply_markup=MENU)
        return
    report = getattr(collect_main, "last_report", {}) or {}
    log(f"  🔎 по кнопке прислано карточек: {sent}")
    if sent >= how_many:
        return                      # пришло сколько просили — лишнего не пишем
    if sent:
        tail = f"Прислал {sent} — больше новых не нашлось."
    else:
        tail = "Ничего нового: всё свежее уже показывал раньше."
    if report.get("skipped_low"):
        tail += f" Ещё {report['skipped_low']} отсеял как слабые."
    send_message(chat_id, tail, reply_markup=MENU)


def handle_message(msg, queue, approved):
    text = (msg.get("text") or "").strip()
    chat_id = (msg.get("chat") or {}).get("id")
    if not text:
        return
    low = text.lower()

    if low == "/cancel":
        store.save("edit.json", {"awaiting": None})
        send_message(chat_id, "Отменено.", reply_markup=MENU)
        return

    # текст, присланный после кнопки «Править», заменяет пост
    if text not in NEWS_WORDS and text not in QUEUE_WORDS and text not in NEXT_WORDS \
            and not text.startswith("/") and apply_edit(text, chat_id, queue):
        return

    if text.startswith("/start"):
        send_message(
            chat_id,
            "Готов к работе.\n\n"
            "<b>🔎 5 новостей</b> — сходить за свежими прямо сейчас\n"
            "<b>📋 Очередь</b> — что накопилось\n"
            "<b>⏭ Опубликовать</b> — отправить в канал следующий одобренный\n\n"
            f"Ваш chat_id: <code>{chat_id}</code>",
            reply_markup=MENU,
        )
    elif text in NEWS_WORDS or low in NEWS_WORDS:
        _fetch_news(chat_id, queue)
    elif text in QUEUE_WORDS or low.startswith("/queue"):
        published = store.load("published.json")
        send_message(
            chat_id,
            f"На модерации: <b>{len(queue['items'])}</b>\n"
            f"Одобрено, ждёт слота: <b>{len(approved['items'])}</b>\n"
            f"Опубликовано всего: <b>{len(published['items'])}</b>",
            reply_markup=MENU,
        )
    elif text in NEXT_WORDS or low.startswith("/next"):
        if not approved["items"]:
            send_message(chat_id, "Очередь публикации пуста.", reply_markup=MENU)
            return
        post = approved["items"].pop(0)
        try:
            publish_now(post)
        except RuntimeError as exc:
            approved["items"].insert(0, post)
            send_message(chat_id, f"Не опубликовалось: {esc(str(exc))[:200]}",
                         reply_markup=MENU)
            return
        send_message(chat_id, f"Опубликовано: {esc(post.get('title', ''))}",
                     reply_markup=MENU)
    elif text.startswith("/id"):
        send_message(chat_id, f"Ваш chat_id: <code>{chat_id}</code>", reply_markup=MENU)
    elif text.startswith("/"):
        send_message(chat_id, "Не знаю такой команды. Пользуйтесь кнопками ниже.",
                     reply_markup=MENU)


def main() -> int:
    require("TELEGRAM_BOT_TOKEN")

    offset_state = store.load("offset.json")
    queue = store.load("queue.json")
    approved = store.load("approved.json")

    updates = get_updates(offset_state["offset"])
    if not updates:
        log("Новых апдейтов нет.")
        return 0

    # Подтверждаем приём СРАЗУ. Иначе любое падение ниже означает, что на
    # следующем запуске Telegram пришлёт те же события снова: повторные ответы
    # на /start, повторные публикации, дубли в очереди.
    offset_state["offset"] = updates[-1]["update_id"] + 1
    if not DRY_RUN:
        store.save("offset.json", offset_state)

    log(f"Апдейтов: {len(updates)}")
    for update in updates:
        try:
            if "callback_query" in update:
                handle_callback(update["callback_query"], queue, approved)
            elif "message" in update:
                handle_message(update["message"], queue, approved)
        except Exception as exc:  # один битый апдейт не должен ронять прогон
            log(f"  ! ошибка обработки: {exc}")
        # пишем очереди после каждого события, чтобы падение не отменило
        # уже применённые решения
        if not DRY_RUN:
            store.save("queue.json", queue)
            store.save("approved.json", approved)

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
        store.save("queue.json", queue)
        store.save("approved.json", approved)
    log("Готово.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
