"""Тонкая обёртка над Telegram Bot API."""
import time

import requests

from .config import BOT_TOKEN, DRY_RUN

API = "https://api.telegram.org/bot{token}/{method}"


def call(method: str, timeout: int = 30, **params):
    if DRY_RUN and method not in ("getUpdates", "getMe"):
        print(f"[DRY_RUN] {method}: {str(params)[:300]}")
        return {"ok": True, "result": {"message_id": 0}}

    for attempt in range(3):
        resp = requests.post(
            API.format(token=BOT_TOKEN, method=method), json=params, timeout=timeout
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


def get_updates(offset: int, timeout: int = 0):
    return call(
        "getUpdates",
        offset=offset,
        timeout=timeout,
        allowed_updates=["callback_query", "message"],
    )


def keyboard(item_id: str):
    return {
        "inline_keyboard": [
            [
                {"text": "✅ В очередь", "callback_data": f"q:{item_id}"},
                {"text": "⚡ Сразу", "callback_data": f"n:{item_id}"},
            ],
            [
                {"text": "♻️ Переписать", "callback_data": f"r:{item_id}"},
                {"text": "🗑 Удалить", "callback_data": f"d:{item_id}"},
            ],
        ]
    }
