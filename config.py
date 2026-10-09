"""Configuration loaded from environment variables."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    """Minimal .env loader (avoids an extra dependency)."""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _first_env(*names: str) -> str:
    """Return the first non-empty value among the given variable names."""
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


# Both naming styles are accepted so an existing .env keeps working.
BOT_TOKEN: str = _first_env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")

ADMIN_IDS: set[int] = {
    int(part)
    for part in _first_env("ADMIN_IDS", "ADMIN_USER_ID", "ADMIN_USER_IDS")
    .replace(" ", "")
    .split(",")
    if part.lstrip("-").isdigit()
}

MAX_REPEAT: int = max(1, _int_env("MAX_REPEAT", 100))
MIN_REPEAT: int = 1
DEFAULT_MODE = "medium"
SPAM_MODES: dict[str, float] = {
    "basic": 2.0,
    "medium": 1.0,
    "aggressive": 0.5,
}
SPAM_MODE_LABELS: dict[str, str] = {
    "basic": "Basic Spam",
    "medium": "Medium Spam",
    "aggressive": "Aggressive Domination",
}
COOLDOWN_SECONDS: int = max(0, _int_env("COOLDOWN_SECONDS", 10))
LICENSE_DAYS: int = _int_env("LICENSE_DAYS", 30)
KEY_PREFIX: str = os.environ.get("KEY_PREFIX", "SPAMBOT").strip().upper() or "SPAMBOT"
DB_PATH: str = os.environ.get("DB_PATH", str(BASE_DIR / "licenses.db"))


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_IDS


def validate() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    if not ADMIN_IDS:
        raise RuntimeError(
            "ADMIN_IDS is not set. Add at least one numeric Telegram user ID."
        )
