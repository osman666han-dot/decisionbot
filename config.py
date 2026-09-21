import os
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = _int("ADMIN_ID", 0)
CHANNEL = os.getenv("CHANNEL", "@kolokotol").strip()
CHANNEL_URL = f"https://t.me/{CHANNEL.lstrip('@')}"

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "decision.db"))
MANUAL_PATH = os.getenv("MANUAL_PATH", str(BASE_DIR / "assets" / "manual.pdf"))

DAILY_LIMIT = _int("DAILY_LIMIT", 5)
MIN_OPTIONS = 3
MAX_OPTIONS = 10
MAX_QUESTION_LEN = 300
MAX_OPTION_LEN = 100
MAX_TOTAL_LEN = 1500

# Сколько ждём первое живое событие, сколько ещё событий берём у победившего источника
LISTEN_TIMEOUT = float(os.getenv("LISTEN_TIMEOUT", "15"))
EXTRA_EVENTS = 2
EXTRA_TIMEOUT = 6.0

try:
    TZ = ZoneInfo("Europe/Kyiv")
except Exception:  # старые базы tzdata
    TZ = ZoneInfo("Europe/Kiev")
