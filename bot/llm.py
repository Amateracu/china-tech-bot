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


def _call(messages: list, temperature: float, timeout: int = 90,
          max_tokens: int = 1200) -> dict:
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
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        },
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"DeepSeek {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


RU_MIN_SHARE = 0.55
RETRY_RU = (
    "ВАЖНО: предыдущий ответ был не на русском. Весь текст поста и заголовок "
    "должны быть на русском языке. Китайские и английские слова допустимы только "
    "как названия компаний и продуктов."
)


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди букв. Ловит случаи, когда модель не перевела текст."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    cyr = sum(1 for c in letters if "\u0400" <= c <= "\u04ff")
    return cyr / len(letters)


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
            if data["publish"]:
                share = cyrillic_share(data["text"])
                if share < RU_MIN_SHARE:
                    if attempt < retries:
                        # ещё попытка с прямым указанием на язык
                        messages[-1] = {
                            "role": "user",
                            "content": _user_prompt(item, variant_hint) + "\n\n" + RETRY_RU,
                        }
                        continue
                    data["publish"] = False
                    data["reason"] = f"текст не на русском (кириллицы {share:.0%})"
            return data
        except (requests.RequestException, json.JSONDecodeError, KeyError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"DeepSeek не ответил: {last_error}")


def ask_json(system: str, user: str, temperature: float = 0.1,
             max_tokens: int = 2500, retries: int = 1) -> dict:
    """Произвольный запрос к модели с ответом в JSON — для служебных задач вроде дедупа."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _call(messages, temperature, timeout=120, max_tokens=max_tokens)
        except (requests.RequestException, json.JSONDecodeError, KeyError, RuntimeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(3)
    raise RuntimeError(f"DeepSeek не ответил: {last_error}")


def render(data: dict, item_url: str, source_name: str) -> str:
    """Собирает финальный текст поста: тело + хэштеги + ссылка на источник."""
    from .config import CHANNEL as ch
    from .util import esc, sanitize_html

    # разметку пишет модель — чистим, иначе один незакрытый тег и Telegram
    # отвергнет пост целиком
    text = sanitize_html(data["text"].strip(), drop_unknown=True).strip()
    tags = [t if t.startswith("#") else f"#{t}" for t in (data.get("tags") or [])]
    if tags and not any(t in text for t in tags):
        text = f"{text}\n\n{' '.join(tags)}"
    cta = (ch.get("cta") or "").strip()
    if cta:
        text = f"{text}\n\n{cta}"
    return f'{text}\n\n<a href="{esc(item_url)}">Источник: {esc(source_name)}</a>'
