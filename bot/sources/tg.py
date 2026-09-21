"""Публичные Telegram-каналы через веб-превью t.me/s/<username> — без API-аккаунта."""
import requests
from bs4 import BeautifulSoup

from ..config import USER_AGENT
from ..util import item_id, strip_html
from .rss import _parse_date


def fetch_telegram(src: dict, timeout: int = 25) -> list:
    from . import Item

    username = src["username"].lstrip("@")
    resp = requests.get(
        f"https://t.me/s/{username}",
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    out = []
    for wrap in soup.select("div.tgme_widget_message_wrap"):
        body = wrap.select_one("div.tgme_widget_message")
        text_el = wrap.select_one("div.tgme_widget_message_text")
        if body is None or text_el is None:
            continue
        text = strip_html(text_el.get_text(" ", strip=True))
        if len(text) < 60:
            continue
        post_id = body.get("data-post", "")
        url = f"https://t.me/{post_id}" if post_id else f"https://t.me/{username}"
        time_el = wrap.select_one("time[datetime]")
        published = _parse_date(time_el["datetime"]) if time_el else None
        title = text.split("\n")[0][:200]
        out.append(
            Item(
                id=item_id(url, title),
                title=title,
                summary=text[:1200],
                url=url,
                source_id=src["id"],
                source_name=src.get("name", f"@{username}"),
                lang=src.get("lang", "ru"),
                china_native=bool(src.get("china_native")),
                weight=float(src.get("weight", 0.7)),
                published=published,
            )
        )
    return out
