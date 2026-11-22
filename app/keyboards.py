from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .texts import Tr, variants

# ---------- callback data (":" is the separator, so values never contain ":") ----------


class LangCB(CallbackData, prefix="lang"):
    code: str


class BookCB(CallbackData, prefix="bk"):
    act: str
    barber: int = 0
    service: int = 0
    day: str = ""  # YYYYMMDD
    time: str = ""  # HHMM


class CliCB(CallbackData, prefix="cl"):
    act: str
    id: int = 0


class BarCB(CallbackData, prefix="br"):
    act: str
    id: int = 0
    day: str = ""
    val: str = ""


class CardCB(CallbackData, prefix="cd"):
    act: str
    client: int


class OwCB(CallbackData, prefix="ow"):
    act: str
    id: int = 0
    val: str = ""


FSM_CANCEL = "fsm_cancel"

# ---------- reply menus ----------

CLIENT_MENU = [["btn_book"], ["btn_my", "btn_contacts"], ["btn_lang"]]
BARBER_MENU = [["btn_today", "btn_tomorrow"], ["btn_late", "btn_block"], ["btn_earn", "btn_shift"]]
OWNER_MENU = [
    ["btn_dash", "btn_salary"],
    ["btn_barbers", "btn_services"],
    ["btn_clients", "btn_stats"],
    ["btn_fill", "btn_settings"],
]

MENU_KEYS = {k for menu in (CLIENT_MENU, BARBER_MENU, OWNER_MENU) for row in menu for k in row}
MENU_TEXTS: set[str] = set()
for _k in MENU_KEYS:
    MENU_TEXTS |= variants(_k)


def main_menu(tr: Tr, roles: set[str]) -> ReplyKeyboardMarkup:
    rows: list[list[str]] = []
    if "barber" in roles:
        rows += BARBER_MENU
    if "owner" in roles:
        rows += OWNER_MENU
    if not roles:
        rows = CLIENT_MENU
    else:
        rows = rows + [["btn_lang"]]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tr(k)) for k in row] for row in rows],
        resize_keyboard=True,
    )


def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data=LangCB(code="uz").pack()),
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data=LangCB(code="ru").pack()),
            ]
        ]
    )


def cancel_kb(tr: Tr) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tr("btn_cancel"), callback_data=FSM_CANCEL)]])


def phone_kb(tr: Tr) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=tr("btn_share_phone"), request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def name_kb(name: str):
    name = (name or "").strip()[:60]
    if not name:
        return ReplyKeyboardRemove()
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=name)]], resize_keyboard=True, one_time_keyboard=True)


def book_btn(tr: Tr) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr("btn_book"), callback_data=BookCB(act="start").pack())]]
    )


def reminder_kb(tr: Tr, appt_id: int, confirmed: bool) -> InlineKeyboardMarkup:
    row = []
    if not confirmed:
        row.append(InlineKeyboardButton(text=tr("btn_confirm_visit"), callback_data=CliCB(act="confirm", id=appt_id).pack()))
    row.append(InlineKeyboardButton(text=tr("btn_cancel_visit"), callback_data=CliCB(act="ask", id=appt_id).pack()))
    return InlineKeyboardMarkup(inline_keyboard=[row])
