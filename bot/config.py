"""Конфиг: .env + yaml-файлы из config/."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"
PROMPTS_DIR = ROOT / "prompts"


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


SOURCES = _yaml("sources.yml")
FILTERS = _yaml("filters.yml")
CHANNEL_CFG = _yaml("channel.yml")

CHANNEL = CHANNEL_CFG.get("channel", {})
PIPELINE = CHANNEL_CFG.get("pipeline", {})
PUBLISHING = CHANNEL_CFG.get("publishing", {})

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
MODERATOR_CHAT_ID = os.environ.get("MODERATOR_CHAT_ID", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def require(*names: str) -> None:
    """Падаем рано и с понятным текстом, если не хватает секретов."""
    missing = [n for n in names if not os.environ.get(n)]
    if missing and not DRY_RUN:
        raise SystemExit("Не заданы переменные окружения: " + ", ".join(missing))
