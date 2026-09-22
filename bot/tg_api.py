"""Тонкая обёртка над Telegram Bot API."""
import json
import time

import requests

from .config import BOT_TOKEN, DRY_RUN

API = "https://api.telegram.org/bot{token}/{method}"
CAPTION_LIMIT = 1024   # лимит Telegram на подпись к фото
TEXT_LIMIT = 4096


def call(method: str, http_timeout: int = 30, **params):
    if DRY_RUN and method not in ("getUpdates", "getMe"):
        print(f"[DRY_RUN] {method}: {str(params)[:300]}")
        return {"ok": True, "result": {"message_id": 0}}

    for attempt in range(3):
        resp = requests.post(
            API.format(token=BOT_TOKEN, method=method), json=params,
            timeout=http_timeout,
        )
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        # 429 — ждём столько, сколько просит Telegram
        if resp.status_code == 429:
            wait = data.get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            continue
        if attempt < 2 and resp.status_code >= 500:
            time.sleep(3)
            continue
        raise RuntimeError(f"Telegram {method} -> {data.get('description')}")
    raise RuntimeError(f"Telegram {method}: превышены попытки")


def send_message(chat_id, text, reply_markup=None, preview_url=None, silent=False):
    params = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_notification": silent,
    }
    if preview_url:
        params["link_preview_options"] = {
            "url": preview_url,
            "prefer_large_media": True,
            "show_above_text": False,
        }
    else:
        params["link_preview_options"] = {"is_disabled": True}
    if reply_markup:
        params["reply_markup"] = reply_markup
    return call("sendMessage", **params)


def send_photo(chat_id, photo_bytes, caption, reply_markup=None,
               silent=False, filename="photo.jpg"):
    """Загружает картинку файлом — так надёжнее, чем давать Telegram ссылку."""
    if DRY_RUN:
        print(f"[DRY_RUN] sendPhoto -> {chat_id}, {len(photo_bytes)} байт, "
              f"подпись {len(caption)} символов")
        return {"message_id": 0}

    data = {
        "chat_id": str(chat_id),
        "caption": caption,
        "parse_mode": "HTML",
        "disable_notification": "true" if silent else "false",
    }
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup)

    for attempt in range(3):
        resp = requests.post(
            API.format(token=BOT_TOKEN, method="sendPhoto"),
            data=data,
            files={"photo": (filename, photo_bytes, "image/jpeg")},
            timeout=120,
        )
        payload = resp.json()
        if payload.get("ok"):
            return payload["result"]
        if resp.status_code == 429:
            time.sleep(payload.get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if attempt < 2 and resp.status_code >= 500:
            time.sleep(3)
            continue
        raise RuntimeError(f"Telegram sendPhoto -> {payload.get('description')}")
    raise RuntimeError("Telegram sendPhoto: превышены попытки")


def edit_caption(chat_id, message_id, caption, reply_markup=None):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption[:CAPTION_LIMIT],
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return call("editMessageCaption", **params)


def edit_message(chat_id, message_id, text, reply_markup=None):
    params = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return call("editMessageText", **params)


def answer_callback(callback_id, text=""):
    return call("answerCallbackQuery", callback_query_id=callback_id, text=text)


def get_updates(offset: int, poll_seconds: int = 0):
    """poll_seconds > 0 — долгий опрос на стороне Telegram; 0 — забрать и сразу выйти.

    Важно: у Telegram свой параметр timeout, и он не должен подменять таймаут HTTP.
    """
    params = {"offset": offset, "allowed_updates": ["callback_query", "message"]}
    if poll_seconds:
        params["timeout"] = poll_seconds
    return call("getUpdates", http_timeout=30 + poll_seconds, **params)


def keyboard(item_id: str):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ В очередь", "callback_data": f"q:{item_id}"},
                {"text": "⚡ Сразу", "callback_data": f"n:{item_id}"},
            ],
            [
                {"text": "♻️ Переписать", "callback_data": f"r:{item_id}"},
                {"text": "✏️ Править", "callback_data": f"e:{item_id}"},
            ],
            [
                {"text": "🗑 Удалить", "callback_data": f"d:{item_id}"},
            ],
        ]
    }
