"""Мелкие утилиты: время, нормализация текста, схожесть заголовков."""
import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TRACKING = ("utm_", "fbclid", "gclid", "yclid", "ref", "from", "spm")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_now(offset_hours: int) -> datetime:
    return now_utc() + timedelta(hours=offset_hours)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def canonical_url(url: str) -> str:
    """Режем трекинг-параметры, чтобы одна новость не попала дважды."""
    if not url:
        return ""
    try:
        parts = urlparse(url)
    except ValueError:
        return url
    query = "&".join(
        p for p in parts.query.split("&")
        if p and not any(p.lower().startswith(t) for t in _TRACKING)
    )
    return urlunparse((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", query, ""))


def item_id(url: str, title: str) -> str:
    key = canonical_url(url) or title
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


_WORD_RE = re.compile(r"[\w一-鿿]+", re.UNICODE)
_STOP = {
    "the", "a", "an", "of", "in", "to", "for", "and", "on", "with", "is", "are",
    "at", "by", "as", "its", "it", "from", "new", "says", "has", "will", "that",
    "и", "в", "на", "с", "по", "для", "от", "о",
}


def tokens(text: str) -> set:
    words = [w.lower() for w in _WORD_RE.findall(text or "")]
    out = set()
    for w in words:
        if w in _STOP or len(w) < 2:
            continue
        # китайский текст не разделён пробелами — режем на биграммы символов
        if any("一" <= ch <= "鿿" for ch in w) and len(w) > 2:
            out.update(w[i:i + 2] for i in range(len(w) - 1))
        else:
            out.add(w)
    return out


def similarity(a: str, b: str) -> float:
    """Жаккар по токенам заголовков. Дёшево и достаточно для ловли дублей."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)
