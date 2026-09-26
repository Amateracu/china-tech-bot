"""Долгоживущий воркер: один запуск живёт несколько часов и делает всё сам.

Запуск: python -m bot.worker

  * держит длинный опрос Telegram — нажатия применяются за секунды;
  * публикует одобренное, когда подходит слот;
  * сам запускает сбор новостей в заданные часы;
  * после каждого изменения коммитит состояние в репозиторий.

Расписание GitHub не используется: следующий запуск заводит workflow
(шаг «Передать смену»), поэтому цепочка не зависит от cron.
"""
import os
import subprocess
import sys
import time
from datetime import timedelta

from . import store
from .config import DRY_RUN, PUBLISHING, require
from .util import local_now, now_utc

RUN_MINUTES = int(os.environ.get("WORKER_MINUTES", "330"))
POLL_SECONDS = int(os.environ.get("WORKER_POLL", "25"))
COLLECT_HOURS = [
    int(h) for h in os.environ.get("COLLECT_HOURS", "8,11,14,17,20").split(",") if h.strip()
]
COMMIT_EVERY = int(os.environ.get("WORKER_COMMIT_EVERY", "30"))  # секунд между push


def log(*args):
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


# ── сохранение состояния в репозиторий ───────────────────────────────────────

def _git(*args, timeout=60):
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=timeout)


def commit_state(reason: str) -> bool:
    """Коммитит state/ и отправляет на GitHub. Неудача не фатальна."""
    if DRY_RUN:
        return False
    try:
        _git("add", "state/")
        staged = _git("diff", "--staged", "--quiet")
        if staged.returncode == 0:
            return False  # нечего сохранять
        _git("commit", "-m", f"state: {reason} {now_utc():%Y-%m-%dT%H:%MZ}")
        for attempt in range(3):
            pull = _git("pull", "--rebase", "--autostash", "origin", _branch(), timeout=120)
            push = _git("push", "origin", f"HEAD:{_branch()}", timeout=120)
            if push.returncode == 0:
                return True
            log(f"  push не прошёл ({attempt + 1}/3): {(push.stderr or pull.stderr)[:120]}")
            time.sleep(5)
    except (subprocess.SubprocessError, OSError) as exc:
        log(f"  состояние не сохранилось: {exc}")
    return False


def _branch() -> str:
    return os.environ.get("GITHUB_REF_NAME") or "main"


# ── отдельные обязанности ────────────────────────────────────────────────────

def pump_telegram() -> bool:
    """Забирает нажатия и применяет их. True, если что-то изменилось."""
    from .moderate import handle_callback, handle_message
    from .tg_api import get_updates

    offset_state = store.load("offset.json")
    try:
        updates = get_updates(offset_state["offset"], poll_seconds=POLL_SECONDS)
    except RuntimeError as exc:
        log(f"  Telegram не ответил: {str(exc)[:120]}")
        time.sleep(5)
        return False
    if not updates:
        return False

    offset_state["offset"] = updates[-1]["update_id"] + 1
    store.save("offset.json", offset_state)

    queue = store.load("queue.json")
    approved = store.load("approved.json")
    log(f"событий: {len(updates)}")
    for update in updates:
        try:
            if "callback_query" in update:
                handle_callback(update["callback_query"], queue, approved)
            elif "message" in update:
                handle_message(update["message"], queue, approved)
        except Exception as exc:
            log(f"  ! ошибка обработки: {exc}")
        store.save("queue.json", queue)
        store.save("approved.json", approved)
    return True


def _warn_blocked(code, human, waiting):
    """Один раз сообщаем, что очередь встала надолго: окно закрылось или лимит."""
    from .config import MODERATOR_CHAT_ID
    from .tg_api import send_message

    state = store.load("worker.json")
    if state.get("block_code") == code:
        return
    state["block_code"] = code
    store.save("worker.json", state)
    if code not in ("window", "limit") or not MODERATOR_CHAT_ID:
        return
    try:
        send_message(MODERATOR_CHAT_ID,
                     f"⏸ Очередь стоит: {human}.\n"
                     f"Ждут публикации: {waiting}. "
                     f"Кнопка «⏭ Опубликовать» отправит следующий пост сразу.")
    except RuntimeError as exc:
        log(f"  предупредить не вышло: {str(exc)[:70]}")


def publish_due() -> bool:
    """Публикует один одобренный пост, если совпали окно, интервал и лимит."""
    from .publish import block_reason, publish_now

    approved = store.load("approved.json")
    if not approved["items"]:
        return False
    published = store.load("published.json")
    code, human = block_reason(published)
    if code:
        _warn_blocked(code, human, len(approved["items"]))
        return False
    _warn_blocked(None, "", 0)

    post = approved["items"].pop(0)
    try:
        publish_now(post)
    except RuntimeError as exc:
        log(f"  публикация не удалась: {str(exc)[:150]}")
        return False
    store.save("approved.json", approved)
    log(f"опубликовано по слоту: {post.get('title', '')[:60]}")
    return True


def collect_due() -> bool:
    """Запускает сбор, когда наступил один из заданных часов."""
    worker_state = store.load("worker.json")
    hour = local_now(int(PUBLISHING.get("timezone_offset", 3)))
    stamp = hour.strftime("%Y-%m-%dT%H")
    if hour.hour not in COLLECT_HOURS or worker_state.get("last_collect") == stamp:
        return False

    log(f"сбор новостей (час {hour.hour})")
    worker_state["last_collect"] = stamp
    store.save("worker.json", worker_state)
    try:
        from .collect import main as collect_main
        collect_main()
    except Exception as exc:
        log(f"  сбор упал: {exc}")
    return True


# ── главный цикл ─────────────────────────────────────────────────────────────

def main() -> int:
    require("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "MODERATOR_CHAT_ID")

    deadline = now_utc() + timedelta(minutes=RUN_MINUTES)
    log(f"смена началась, работаю до {deadline:%H:%M} UTC, "
        f"сбор в часы {COLLECT_HOURS} по Минску")

    dirty, last_push = False, time.monotonic()
    while now_utc() < deadline:
        if pump_telegram():
            dirty = True
        if publish_due():
            dirty = True
        if collect_due():
            dirty = True

        if dirty and time.monotonic() - last_push >= COMMIT_EVERY:
            if commit_state("worker"):
                log("состояние сохранено")
            dirty, last_push = False, time.monotonic()

    if dirty:
        commit_state("worker")
    log("смена окончена")
    return 0


if __name__ == "__main__":
    sys.exit(main())
