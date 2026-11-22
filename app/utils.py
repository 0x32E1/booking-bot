import re
from datetime import date, datetime, time
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import Message

from .texts import WEEKDAYS, Tr


def h(value) -> str:
    """Escape user-provided text for HTML parse mode."""
    return escape(str(value)) if value is not None else ""


def money(amount, tr: Tr) -> str:
    return f"{int(amount or 0):,}".replace(",", " ") + " " + tr("currency")


def fmt_day(d: date, tr: Tr) -> str:
    return f"{WEEKDAYS[tr.lang][d.weekday()]} {d:%d.%m}"


def fmt_dt(dt: datetime, tr: Tr) -> str:
    return f"{fmt_day(dt.date(), tr)} {dt:%H:%M}"


def raw_client_name(appt, tr: Tr) -> str:
    """Client name for buttons (no HTML)."""
    if appt.client is not None and appt.client.name:
        return appt.client.name
    return tr("walkin_client")


def client_name(appt, tr: Tr) -> str:
    """Client name for HTML messages."""
    return h(raw_client_name(appt, tr))


def parse_int(text: str | None) -> int | None:
    cleaned = re.sub(r"[\s,._']", "", text or "")
    if not cleaned.isdigit():
        return None
    return int(cleaned)


def parse_day(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def parse_hhmm(value: str) -> time | None:
    try:
        return datetime.strptime(value, "%H%M").time()
    except (ValueError, TypeError):
        return None


_HOURS_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})\s*$")


def parse_hours(text: str) -> tuple[str, str] | None:
    """'9:00-20:00' -> ('09:00', '20:00')"""
    m = _HOURS_RE.match(text or "")
    if not m:
        return None
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 <= 23 and 0 <= h2 <= 23 and 0 <= m1 <= 59 and 0 <= m2 <= 59):
        return None
    if (h2, m2) <= (h1, m1):
        return None
    return f"{h1:02d}:{m1:02d}", f"{h2:02d}:{m2:02d}"


def normalize_phone(text: str | None) -> str | None:
    """Uzbek numbers -> +998XXXXXXXXX. Other international numbers kept as +digits."""
    digits = re.sub(r"\D", "", text or "")
    if len(digits) == 9:
        return "+998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return "+" + digits
    if 10 <= len(digits) <= 15 and (text or "").strip().startswith("+"):
        return "+" + digits
    return None


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    try:
        await bot.send_message(chat_id, text, **kwargs)
        return True
    except TelegramAPIError:
        return False


async def safe_edit(message: Message, text: str, reply_markup=None, **kwargs) -> None:
    """Edit a bot message; if it can't be edited (photo, too old), send a new one."""
    try:
        await message.edit_text(text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await message.answer(text, reply_markup=reply_markup, **kwargs)
