"""Парсер RSS 2.0 / Atom на stdlib — без feedparser, чтобы держать зависимости тонкими."""
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

import requests

from ..config import USER_AGENT
from ..util import canonical_url, item_id, strip_html

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _text(node, *paths) -> str:
    for p in paths:
        found = node.find(p, NS)
        if found is not None:
            if found.text:
                return found.text.strip()
            href = found.get("href")
            if href:
                return href.strip()
    return ""


def _parse_date(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    if _ISO_RE.match(raw):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def fetch_rss(src: dict, timeout: int = 25) -> list:
    from . import Item

    resp = requests.get(
        src["url"],
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
    )
    resp.raise_for_status()
    body = resp.content
    # некоторые китайские фиды отдают битую кодировку в заголовке
    root = ET.fromstring(re.sub(rb"^\s+", b"", body))

    nodes = root.findall(".//item")
    atom = False
    if not nodes:
        nodes = root.findall(".//atom:entry", NS)
        atom = True

    out = []
    for node in nodes:
        title = strip_html(_text(node, "title", "atom:title"))
        if not title:
            continue
        link = _text(node, "link", "atom:link[@rel='alternate']", "atom:link", "guid")
        summary = strip_html(
            _text(node, "description", "atom:summary", "content:encoded", "atom:content")
        )[:1200]
        published = _parse_date(
            _text(node, "pubDate", "atom:published", "atom:updated", "dc:date")
        )
        url = canonical_url(link)
        out.append(
            Item(
                id=item_id(url, title),
                title=title,
                summary=summary,
                url=url or link,
                source_id=src["id"],
                source_name=src["name"],
                lang=src.get("lang", "en"),
                china_native=bool(src.get("china_native")),
                weight=float(src.get("weight", 1.0)),
                published=published,
            )
        )
    _ = atom
    return out
