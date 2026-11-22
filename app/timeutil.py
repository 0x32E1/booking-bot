from datetime import date, datetime

from .config import TIMEZONE


def now() -> datetime:
    """Current local time (shop timezone), naive, no microseconds."""
    return datetime.now(TIMEZONE).replace(tzinfo=None, microsecond=0)


def today() -> date:
    return now().date()
