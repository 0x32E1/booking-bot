import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

# SQLite by default (no extra server, ~0 RAM). For PostgreSQL:
# DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/barber
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///data/bot.db")
TIMEZONE = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
# Shown to shop owners when their subscription expires
ADMIN_CONTACT = os.getenv("ADMIN_CONTACT", "")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
