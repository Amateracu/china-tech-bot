"""Картинка к посту: сначала фото источника, иначе — своя карточка с заголовком."""
import io
import re
from urllib.parse import urljoin, urlparse

import requests

from .config import CHANNEL_CFG, USER_AGENT

MEDIA = CHANNEL_CFG.get("media", {}) or {}

MODE = MEDIA.get("mode", "source_or_card")
MIN_WIDTH = int(MEDIA.get("min_width", 400))
MIN_HEIGHT = int(MEDIA.get("min_height", 220))
MAX_BYTES = int(MEDIA.get("max_bytes", 9_000_000))
HTML_BUDGET = 400_000  # сколько байт страницы читаем ради og:image

_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](og:image(?::url)?|twitter:image(?::src)?)["\'][^>]*>',
    re.I,
)
_CONTENT = re.compile(r'content=["\']([^"\']+)["\']', re.I)
_BAD_HINTS = ("logo", "sprite", "placeholder", "avatar", "icon", "1x1", "pixel", "blank")


# ── поиск URL картинки ───────────────────────────────────────────────────────

def og_image(page_url: str, timeout: int = 20) -> str:
    """Читает начало HTML статьи и достаёт og:image / twitter:image."""
    if not page_url:
        return ""
    try:
        with requests.get(
            page_url, timeout=timeout, stream=True,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"},
        ) as resp:
            resp.raise_for_status()
            if "html" not in resp.headers.get("Content-Type", ""):
                return ""
            chunks, total = [], 0
            for chunk in resp.iter_content(32_768):
                chunks.append(chunk)
                total += len(chunk)
                if total >= HTML_BUDGET:
                    break
            html = b"".join(chunks).decode("utf-8", "ignore")
    except requests.RequestException:
        return ""

    for match in _META.finditer(html):
        content = _CONTENT.search(match.group(0))
        if not content:
            continue
        url = content.group(1).strip()
        if url and not any(bad in url.lower() for bad in _BAD_HINTS):
            return urljoin(page_url, url)
    return ""


def pick_image_url(item) -> str:
    """Сначала то, что дал фид, потом og:image со страницы статьи."""
    if MODE in ("off", "card_only"):
        return ""
    candidate = (getattr(item, "image", "") or "").strip()
    if candidate and not any(bad in candidate.lower() for bad in _BAD_HINTS):
        return urljoin(item.url or "", candidate)
    return og_image(item.url)


# ── загрузка и проверка ──────────────────────────────────────────────────────

def fetch_image(url: str, referer: str = "", timeout: int = 25):
    """Скачивает картинку и проверяет, что она годится для поста. -> bytes | None"""
    if not url:
        return None
    headers = {"User-Agent": USER_AGENT, "Accept": "image/*,*/*"}
    if referer:
        headers["Referer"] = referer
    else:
        parts = urlparse(url)
        headers["Referer"] = f"{parts.scheme}://{parts.netloc}/"
    try:
        resp = requests.get(url, timeout=timeout, headers=headers, stream=True)
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if "image" not in ctype or "svg" in ctype:
            return None
        data, total = [], 0
        for chunk in resp.iter_content(65_536):
            data.append(chunk)
            total += len(chunk)
            if total > MAX_BYTES:
                return None
        blob = b"".join(data)
    except requests.RequestException:
        return None

    try:
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            width, height = im.size
            fmt = (im.format or "").upper()
    except Exception:
        return None

    if width < MIN_WIDTH or height < MIN_HEIGHT:
        return None
    if fmt not in ("JPEG", "PNG", "WEBP"):
        return None
    if fmt == "WEBP":  # Telegram не принимает webp как фото
        blob = _to_jpeg(blob)
    return blob


def _to_jpeg(blob: bytes) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(blob)) as im:
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=88)
        return out.getvalue()


# ── своя карточка ────────────────────────────────────────────────────────────

CARD_W, CARD_H = 1200, 630
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)
_CJK_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)


def _font(size: int, paths=_FONT_PATHS, index=0):
    from PIL import ImageFont
    import os
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size, index=index)
            except OSError:
                continue
    return None


# Китайские названия источников латинский шрифт карточки рисует квадратиками,
# поэтому на карточке пишем их латиницей.
_LATIN_LABELS = {
    "IT之家": "ITHome", "36氪": "36Kr", "爱范儿": "ifanr", "量子位": "QbitAI",
    "虎嗅": "Huxiu", "快科技": "MyDrivers", "机器之心": "Jiqizhixin", "钛媒体": "TMTPost",
}


def card_label(source_name: str) -> str:
    """Подпись источника на карточке: только символы, которые точно отрисуются."""
    name = (source_name or "").strip()
    if name in _LATIN_LABELS:
        return _LATIN_LABELS[name]
    if all(ch.isascii() or "\u0400" <= ch <= "\u04ff" for ch in name):
        return name
    # неизвестное китайское название: оставляем латиницу и цифры, если они есть
    latin = "".join(ch for ch in name if ch.isascii()).strip()
    return latin


def make_card(title: str, source_name: str = "") -> bytes:
    """Фирменная карточка с заголовком — когда у источника картинки нет."""
    from PIL import Image, ImageDraw

    bg = tuple(MEDIA.get("card_bg", (166, 26, 32)))
    fg = tuple(MEDIA.get("card_fg", (245, 241, 233)))

    img = Image.new("RGB", (CARD_W, CARD_H), bg)
    draw = ImageDraw.Draw(img)

    cjk = _font(int(CARD_H * 0.72), _CJK_PATHS)
    if cjk is not None:  # водяной знак 明 — тот же, что на аватарке
        box = draw.textbbox((0, 0), "明", font=cjk)
        gw, gh = box[2] - box[0], box[3] - box[1]
        draw.text((CARD_W - gw * 0.92, CARD_H * 0.52 - gh / 2 - box[1]),
                  "明", font=cjk, fill=_mix(bg, fg, 0.13))

    size = 58
    font = _font(size)
    if font is None:
        return _flat_card(img)

    max_w = int(CARD_W * 0.76)
    lines = _wrap(draw, title.strip(), font, max_w)
    while len(lines) > 5 and size > 34:
        size -= 6
        font = _font(size)
        lines = _wrap(draw, title.strip(), font, max_w)
    lines = lines[:5]

    line_h = int(size * 1.34)
    block_h = line_h * len(lines)
    y = (CARD_H - block_h) / 2 - CARD_H * 0.03
    for line in lines:
        draw.text((CARD_W * 0.08, y), line, font=font, fill=fg)
        y += line_h

    small = _font(28)
    label = card_label(source_name)
    if small and label:
        draw.text((CARD_W * 0.08, CARD_H - 78), label.upper(),
                  font=small, fill=_mix(bg, fg, 0.72))
    draw.rectangle([CARD_W * 0.08, CARD_H * 0.5 - block_h / 2 - CARD_H * 0.09,
                    CARD_W * 0.08 + 96, CARD_H * 0.5 - block_h / 2 - CARD_H * 0.09 + 8], fill=fg)

    return _flat_card(img)


def _flat_card(img) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _wrap(draw, text: str, font, max_w: int) -> list:
    words, lines, current = text.split(), [], ""
    for word in words:
        probe = f"{current} {word}".strip()
        if draw.textlength(probe, font=font) <= max_w or not current:
            current = probe
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ── то, что вызывает бот ─────────────────────────────────────────────────────

def resolve(title: str, image_url: str, page_url: str, source_name: str):
    """Возвращает (bytes | None, откуда): 'source', 'card' или ''."""
    if MODE == "off":
        return None, ""
    if MODE != "card_only":
        blob = fetch_image(image_url, referer=page_url)
        if blob:
            return blob, "source"
        if MODE == "source_only":
            return None, ""
    try:
        return make_card(title, source_name), "card"
    except Exception as exc:
        print(f"    карточка не нарисовалась: {exc}", flush=True)
        return None, ""
