"""Рерайт новости в пост через DeepSeek."""
import json
import time

import requests

from .config import (CHANNEL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
                     DEEPSEEK_MODEL, PROMPTS_DIR)
from .util import truncate

_LANG_HINT = {
    "zh": "Исходник на китайском — переведи смысл, не переводи дословно.",
    "en": "Исходник на английском — переведи смысл, не калькируй английский синтаксис.",
    "ru": "Исходник на русском — обязательно перепиши своими словами, не копируй фразы.",
}


def _system_prompt() -> str:
    template = (PROMPTS_DIR / "post_ru.md").read_text(encoding="utf-8")
    return template.format(
        topic=CHANNEL.get("topic", "технологии Китая"),
        audience=CHANNEL.get("audience", "русскоязычная аудитория"),
        voice=CHANNEL.get("voice", "нейтрально и по делу"),
        length=CHANNEL.get("length", "600-900 символов"),
        emoji=CHANNEL.get("emoji", "без эмодзи"),
        hashtags=CHANNEL.get("hashtags", 2),
        rubrics="\n".join(f"  {r}" for r in CHANNEL.get("rubrics") or []),
    )


def _user_prompt(item, variant_hint: str = "") -> str:
    published = item.published.strftime("%Y-%m-%d %H:%M UTC") if item.published else "неизвестно"
    parts = [
        f"Источник: {item.source_name}",
        f"Дата: {published}",
        f"Заголовок: {item.title}",
        f"Текст: {truncate(item.summary, 2000) or '(в фиде только заголовок)'}",
        _LANG_HINT.get(item.lang, ""),
    ]
    if variant_hint:
        parts.append(variant_hint)
    return "\n".join(p for p in parts if p)


def _call(messages: list, temperature: float, timeout: int = 90) -> dict:
    resp = requests.post(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"DeepSeek {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


def write_post(item, variant_hint: str = "", temperature: float = 0.6, retries: int = 2) -> dict:
    """Возвращает dict с ключами publish/score/title/text/tags/reason."""
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _user_prompt(item, variant_hint)},
    ]
    last_error = None
    for attempt in range(retries + 1):
        try:
            data = _call(messages, temperature)
            data.setdefault("publish", False)
            data.setdefault("score", 0)
            data.setdefault("tags", [])
            data["title"] = (data.get("title") or item.title)[:80]
            data["text"] = (data.get("text") or "").strip()
            if data["publish"] and not data["text"]:
                data["publish"] = False
                data["reason"] = "модель вернула пустой текст"
            return data
        except (requests.RequestException, json.JSONDecodeError, KeyError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"DeepSeek не ответил: {last_error}")


def render(data: dict, item_url: str, source_name: str) -> str:
    """Собирает финальный текст поста: тело + хэштеги + ссылка на источник."""
    from .config import CHANNEL as ch
    from .util import esc

    text = data["text"].strip()
    tags = [t if t.startswith("#") else f"#{t}" for t in (data.get("tags") or [])]
    if tags and not any(t in text for t in tags):
        text = f"{text}\n\n{' '.join(tags)}"
    cta = (ch.get("cta") or "").strip()
    if cta:
        text = f"{text}\n\n{cta}"
    return f'{text}\n\n<a href="{esc(item_url)}">Источник: {esc(source_name)}</a>'
