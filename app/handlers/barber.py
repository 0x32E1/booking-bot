"""Barber panel: day schedule, mark done/no-show, walk-ins, manual bookings,
running late, blocking time, earnings, shift check-in."""

from datetime import datetime, time, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..filters import RoleFilter
from ..keyboards import FSM_CANCEL, MENU_TEXTS, BarCB, CardCB, book_btn, cancel_kb
from ..models import Appointment, Barber, Block, ClientNote, Service, Shift, Shop, User
from ..notify import notify_user
from ..services.clients import find_by_phone
from ..services.reports import barber_stats, period_range
from ..services.slots import ACTIVE, free_slots, get_hours, hm
from ..texts import Tr, variants
from ..timeutil import now, today
from ..utils import (
    client_name,
    fmt_day,
    fmt_dt,
    h,
    money,
    normalize_phone,
    parse_day,
    parse_hhmm,
    parse_int,
    raw_client_name,
    safe_edit,
)
from ..views import active_services

router = Router(name="barber")
router.message.filter(RoleFilter("barber"))
router.callback_query.filter(RoleFilter("barber"))

ICON = {"booked": "⏳", "confirmed": "👍", "done": "✅", "no_show": "🚫", "cancelled": "❌"}


class BarberStates(StatesGroup):
    price = State()
    tip = State()
    add_phone = State()
    add_name = State()


async def _own_appt(session: AsyncSession, barber: Barber, appt_id: int) -> Appointment | None:
    a = await session.get(Appointment, appt_id)
    return a if a and a.barber_id == barber.id else None


async def _service(session: AsyncSession, shop: Shop, service_id: int) -> Service | None:
    s = await session.get(Service, service_id)
    return s if s and s.shop_id == shop.id and s.active else None


# ======================= day schedule =======================


async def render_day(session: AsyncSession, barber: Barber, day, tr: Tr):
    start = datetime.combine(day, time.min)
    end = start + timedelta(days=1)
    appts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.barber_id == barber.id,
                Appointment.start_at >= start,
                Appointment.start_at < end,
                Appointment.status != "cancelled",
            )
            .order_by(Appointment.start_at)
        )
    ).all()
    blocks = (
        await session.scalars(
            select(Block)
            .where(Block.barber_id == barber.id, Block.start_at < end, Block.end_at > start)
            .order_by(Block.start_at)
        )
    ).all()
    wh = (await get_hours(session, barber.id)).get(day.weekday())
    current = now()

    lines = [tr("day_title", day=fmt_day(day, tr), name=h(barber.name))]
    lines.append(tr("day_hours", opens=wh.opens, closes=wh.closes) if wh else tr("day_off"))
    lines.append("")
    items = [(a.start_at, 0, a) for a in appts] + [(b.start_at, 1, b) for b in blocks]
    items.sort(key=lambda x: (x[0], x[1]))
    for _, kind, obj in items:
        if kind == 0:
            warn = "⚠️ " if obj.status in ACTIVE and obj.end_at < current else ""
            lines.append(
                f"{warn}{ICON[obj.status]} <b>{obj.start_at:%H:%M}–{obj.end_at:%H:%M}</b> "
                f"{client_name(obj, tr)} · {h(obj.service.name)}"
            )
        else:
            lines.append(f"⛔ <b>{obj.start_at:%H:%M}–{obj.end_at:%H:%M}</b> {tr('blocked_word')}")
    if not appts:
        lines.append(tr("no_appts"))
    done = [a for a in appts if a.status == "done"]
    lines.append("")
    lines.append(
        tr("day_summary", total=len(appts), done=len(done), revenue=money(sum(a.price or 0 for a in done), tr))
    )

    b = InlineKeyboardBuilder()
    for a in appts:
        b.button(text=f"{ICON[a.status]} {a.start_at:%H:%M} {raw_client_name(a, tr)}"[:40], callback_data=BarCB(act="view", id=a.id))
    b.adjust(2)
    ds = day.strftime("%Y%m%d")
    action_row = [InlineKeyboardButton(text=tr("btn_add_booking"), callback_data=BarCB(act="add", day=ds).pack())]
    if day == current.date():
        action_row.append(InlineKeyboardButton(text=tr("btn_walkin"), callback_data=BarCB(act="walkin").pack()))
    b.row(*action_row)
    b.row(
        InlineKeyboardButton(text="◀️", callback_data=BarCB(act="day", day=(day - timedelta(days=1)).strftime("%Y%m%d")).pack()),
        InlineKeyboardButton(text="🔄", callback_data=BarCB(act="day", day=ds).pack()),
        InlineKeyboardButton(text="▶️", callback_data=BarCB(act="day", day=(day + timedelta(days=1)).strftime("%Y%m%d")).pack()),
    )
    return "\n".join(lines), b.as_markup()


@router.message(F.text.in_(variants("btn_today")))
async def today_view(message: Message, session: AsyncSession, barber: Barber, tr: Tr, state: FSMContext):
    await state.clear()
    text, kb = await render_day(session, barber, today(), tr)
    await message.answer(text, reply_markup=kb)


@router.message(F.text.in_(variants("btn_tomorrow")))
async def tomorrow_view(message: Message, session: AsyncSession, barber: Barber, tr: Tr, state: FSMContext):
    await state.clear()
    text, kb = await render_day(session, barber, today() + timedelta(days=1), tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(BarCB.filter(F.act == "day"))
async def day_cb(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    day = parse_day(callback_data.day) or today()
    text, kb = await render_day(session, barber, day, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


# ======================= one appointment =======================


async def render_appt(session: AsyncSession, appt: Appointment, tr: Tr):
    lines = [tr("appt_title", when=fmt_dt(appt.start_at, tr), end=f"{appt.end_at:%H:%M}")]
    price = appt.price if appt.price is not None else appt.service.price
    lines.append(tr("appt_service", service=h(appt.service.name), price=money(price, tr)))
    lines.append(tr("appt_status", status=tr("st_" + appt.status)))
    c = appt.client
    if c is not None:
        visits = await session.scalar(
            select(func.count()).select_from(Appointment).where(Appointment.client_id == c.id, Appointment.status == "done")
        )
        lines.append(tr("appt_client", name=h(c.name or "—"), phone=h(c.phone or "—"), visits=visits or 0))
        if c.no_show_count:
            lines.append(tr("appt_noshows", n=c.no_show_count))
        note = await session.scalar(
            select(ClientNote)
            .where(ClientNote.client_id == c.id, ClientNote.text.is_not(None))
            .order_by(ClientNote.id.desc())
            .limit(1)
        )
        if note:
            lines.append(tr("appt_last_note", note=h(note.text)))
    else:
        lines.append(tr("walkin_client"))
    if appt.status == "done" and appt.tip:
        lines.append(tr("appt_tip", tip=money(appt.tip, tr)))

    b = InlineKeyboardBuilder()
    if appt.status in ACTIVE:
        b.button(text=tr("btn_done"), callback_data=BarCB(act="done", id=appt.id))
        b.button(text=tr("btn_noshow"), callback_data=BarCB(act="noshow", id=appt.id))
    if c is not None:
        b.button(text=tr("btn_card"), callback_data=CardCB(act="view", client=c.id))
    if appt.status in ACTIVE:
        b.button(text=tr("btn_cancel_appt"), callback_data=BarCB(act="cancel_ask", id=appt.id))
    b.button(text=tr("btn_back"), callback_data=BarCB(act="day", day=appt.start_at.strftime("%Y%m%d")))
    b.adjust(2)
    return "\n".join(lines), b.as_markup()


@router.callback_query(BarCB.filter(F.act == "view"))
async def appt_view(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    a = await _own_appt(session, barber, callback_data.id)
    if a is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    text, kb = await render_appt(session, a, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


# ---------- done: price -> tip -> save ----------


def _price_kb(tr: Tr, price: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=f"✔️ {money(price, tr)}", callback_data=BarCB(act="price_ok", val=str(price)))
    b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
    b.adjust(1)
    return b.as_markup()


@router.callback_query(BarCB.filter(F.act == "done"))
async def done_start(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr, state: FSMContext):
    a = await _own_appt(session, barber, callback_data.id)
    if a is None or a.status not in ACTIVE:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(BarberStates.price)
    await state.update_data(appt_id=a.id, walkin_service=None)
    price = a.price if a.price is not None else a.service.price
    await cb.message.answer(
        tr("ask_price", client=client_name(a, tr), service=h(a.service.name)), reply_markup=_price_kb(tr, price)
    )
    await cb.answer()


@router.callback_query(BarberStates.price, BarCB.filter(F.act == "price_ok"))
async def price_ok(cb: CallbackQuery, callback_data: BarCB, state: FSMContext, tr: Tr):
    await _ask_tip(cb.message, state, int(callback_data.val or 0), tr)
    await cb.answer()


@router.message(BarberStates.price, F.text, ~F.text.in_(MENU_TEXTS))
async def price_text(message: Message, state: FSMContext, tr: Tr):
    value = parse_int(message.text)
    if value is None or value > 100_000_000:
        await message.answer(tr("number_invalid"))
        return
    await _ask_tip(message, state, value, tr)


async def _ask_tip(target: Message, state: FSMContext, price: int, tr: Tr):
    await state.update_data(price=price)
    await state.set_state(BarberStates.tip)
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_no_tip"), callback_data=BarCB(act="tip0"))
    b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
    b.adjust(1)
    await target.answer(tr("ask_tip"), reply_markup=b.as_markup())


@router.callback_query(BarberStates.tip, BarCB.filter(F.act == "tip0"))
async def tip_zero(cb: CallbackQuery, session: AsyncSession, shop: Shop, barber: Barber, state: FSMContext, tr: Tr):
    await _finish(cb.message, session, shop, barber, state, tr, 0)
    await cb.answer()


@router.message(BarberStates.tip, F.text, ~F.text.in_(MENU_TEXTS))
async def tip_text(message: Message, session: AsyncSession, shop: Shop, barber: Barber, state: FSMContext, tr: Tr):
    value = parse_int(message.text)
    if value is None or value > 100_000_000:
        await message.answer(tr("number_invalid"))
        return
    await _finish(message, session, shop, barber, state, tr, value)


async def _finish(target: Message, session: AsyncSession, shop: Shop, barber: Barber, state: FSMContext, tr: Tr, tip: int):
    data = await state.get_data()
    await state.clear()
    price = int(data.get("price") or 0)

    if data.get("walkin_service"):
        service = await _service(session, shop, data["walkin_service"])
        if service is None:
            await target.answer(tr("not_available"))
            return
        start = now().replace(second=0)
        start -= timedelta(minutes=start.minute % 5)
        session.add(
            Appointment(
                shop_id=shop.id,
                barber_id=barber.id,
                client_id=None,
                service_id=service.id,
                start_at=start,
                end_at=start + timedelta(minutes=service.duration),
                status="done",
                price=price,
                tip=tip,
                reminded_24=True,
                reminded_2=True,
            )
        )
        await session.commit()
        await target.answer(tr("walkin_saved", service=h(service.name), price=money(price, tr), tip=money(tip, tr)))
        return

    a = await _own_appt(session, barber, data.get("appt_id") or 0)
    if a is None or a.status not in ACTIVE:
        await target.answer(tr("not_available"))
        return
    a.status = "done"
    a.price = price
    a.tip = tip
    await session.commit()
    kb = None
    if a.client is not None:
        b = InlineKeyboardBuilder()
        b.button(text=tr("btn_add_note"), callback_data=CardCB(act="note", client=a.client.id))
        b.button(text=tr("btn_add_photo"), callback_data=CardCB(act="photo", client=a.client.id))
        b.adjust(2)
        kb = b.as_markup()
    await target.answer(
        tr("done_ok", client=client_name(a, tr), price=money(price, tr), tip=money(tip, tr)), reply_markup=kb
    )


# ---------- no-show ----------


@router.callback_query(BarCB.filter(F.act == "noshow"))
async def no_show(
    cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, shop: Shop, barber: Barber, tr: Tr, bot: Bot
):
    a = await _own_appt(session, barber, callback_data.id)
    if a is None or a.status not in ACTIVE:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    if a.start_at > now() + timedelta(minutes=5):
        await cb.answer(tr("too_early"), show_alert=True)
        return
    a.status = "no_show"
    blocked_now = False
    c = a.client
    if c is not None:
        c.no_show_count += 1
        if shop.max_no_shows > 0 and c.no_show_count >= shop.max_no_shows and not c.blocked:
            c.blocked = True
            blocked_now = True
    await session.commit()
    text, kb = await render_appt(session, a, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer(tr("noshow_marked") + ("\n" + tr("client_now_blocked") if blocked_now else ""), show_alert=True)
    if blocked_now:
        await notify_user(bot, c, "you_blocked", phone=h(shop.phone or "—"))


# ---------- cancel by barber ----------


@router.callback_query(BarCB.filter(F.act == "cancel_ask"))
async def cancel_ask(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    a = await _own_appt(session, barber, callback_data.id)
    if a is None or a.status not in ACTIVE:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr("btn_yes_cancel"), callback_data=BarCB(act="cancel_yes", id=a.id).pack()),
                InlineKeyboardButton(text=tr("btn_no_keep"), callback_data=BarCB(act="view", id=a.id).pack()),
            ]
        ]
    )
    await safe_edit(cb.message, tr("barber_cancel_confirm", client=client_name(a, tr), when=fmt_dt(a.start_at, tr)), kb)
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "cancel_yes"))
async def cancel_yes(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr, bot: Bot):
    a = await _own_appt(session, barber, callback_data.id)
    if a is None or a.status not in ACTIVE:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    a.status = "cancelled"
    await session.commit()
    start = a.start_at
    await notify_user(
        bot,
        a.client,
        "barber_cancelled",
        markup=book_btn,
        barber=h(barber.name),
        when=lambda t: fmt_dt(start, t),
    )
    text, kb = await render_day(session, barber, start.date(), tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer(tr("appt_cancelled"))


# ======================= walk-in (client in the chair right now) =======================


@router.callback_query(BarCB.filter(F.act == "walkin"))
async def walkin(cb: CallbackQuery, session: AsyncSession, shop: Shop, tr: Tr):
    services = await active_services(session, shop.id)
    if not services:
        await cb.answer(tr("no_services"), show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for s in services:
        b.button(text=f"{s.name} · {money(s.price, tr)}", callback_data=BarCB(act="walkin_s", id=s.id))
    b.button(text=tr("btn_back"), callback_data=BarCB(act="day", day=today().strftime("%Y%m%d")))
    b.adjust(1)
    await safe_edit(cb.message, tr("walkin_choose"), b.as_markup())
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "walkin_s"))
async def walkin_service(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    s = await _service(session, shop, callback_data.id)
    if s is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(BarberStates.price)
    await state.update_data(appt_id=None, walkin_service=s.id)
    await cb.message.answer(tr("ask_price_walkin", service=h(s.name)), reply_markup=_price_kb(tr, s.price))
    await cb.answer()


# ======================= manual booking (client called by phone) =======================


@router.callback_query(BarCB.filter(F.act == "add"))
async def add_choose_service(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, shop: Shop, tr: Tr):
    day = parse_day(callback_data.day)
    services = await active_services(session, shop.id)
    if day is None or not services:
        await cb.answer(tr("no_services") if not services else tr("not_available"), show_alert=True)
        return
    b = InlineKeyboardBuilder()
    for s in services:
        b.button(text=f"{s.name} · {s.duration} {tr('min')}", callback_data=BarCB(act="add_s", id=s.id, day=callback_data.day))
    b.button(text=tr("btn_back"), callback_data=BarCB(act="day", day=callback_data.day))
    b.adjust(1)
    await safe_edit(cb.message, tr("add_choose_service", day=fmt_day(day, tr)), b.as_markup())
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "add_s"))
async def add_choose_time(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, shop: Shop, barber: Barber, tr: Tr):
    day = parse_day(callback_data.day)
    s = await _service(session, shop, callback_data.id)
    if day is None or s is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    slots = await free_slots(session, barber.id, day, s.duration, shop.slot_step)
    b = InlineKeyboardBuilder()
    for t in slots:
        b.button(text=f"{t:%H:%M}", callback_data=BarCB(act="add_t", id=s.id, day=callback_data.day, val=f"{t:%H%M}"))
    b.adjust(4)
    b.row(InlineKeyboardButton(text=tr("btn_back"), callback_data=BarCB(act="add", day=callback_data.day).pack()))
    text = tr("choose_time", day=fmt_day(day, tr)) if slots else tr("no_slots")
    await safe_edit(cb.message, text, b.as_markup())
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "add_t"))
async def add_time_picked(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    day, t = parse_day(callback_data.day), parse_hhmm(callback_data.val)
    s = await _service(session, shop, callback_data.id)
    if day is None or t is None or s is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(BarberStates.add_phone)
    await state.update_data(service_id=s.id, start=datetime.combine(day, t).isoformat(), phone=None)
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_skip_phone"), callback_data=BarCB(act="add_nophone"))
    b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
    b.adjust(1)
    await cb.message.answer(tr("ask_client_phone"), reply_markup=b.as_markup())
    await cb.answer()


@router.message(BarberStates.add_phone, F.text, ~F.text.in_(MENU_TEXTS))
async def add_phone(
    message: Message, session: AsyncSession, shop: Shop, barber: Barber, state: FSMContext, tr: Tr, bot: Bot
):
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(tr("phone_invalid"))
        return
    existing = await find_by_phone(session, shop.id, phone)
    if existing is not None:
        await _create_manual(message, session, shop, barber, state, tr, bot, existing)
        return
    await state.update_data(phone=phone)
    await state.set_state(BarberStates.add_name)
    await message.answer(tr("ask_client_name"), reply_markup=cancel_kb(tr))


@router.callback_query(BarberStates.add_phone, BarCB.filter(F.act == "add_nophone"))
async def add_no_phone(cb: CallbackQuery, state: FSMContext, tr: Tr):
    await state.set_state(BarberStates.add_name)
    await cb.message.answer(tr("ask_client_name"), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(BarberStates.add_name, F.text, ~F.text.in_(MENU_TEXTS))
async def add_name(message: Message, session: AsyncSession, shop: Shop, barber: Barber, state: FSMContext, tr: Tr, bot: Bot):
    name = " ".join(message.text.split())
    if len(name) < 2 or len(name) > 60:
        await message.answer(tr("name_invalid"))
        return
    data = await state.get_data()
    client = User(shop_id=shop.id, tg_id=None, name=name, phone=data.get("phone"))
    session.add(client)
    await session.flush()
    await _create_manual(message, session, shop, barber, state, tr, bot, client)


async def _create_manual(
    target: Message, session: AsyncSession, shop: Shop, barber: Barber, state: FSMContext, tr: Tr, bot: Bot, client: User
):
    data = await state.get_data()
    await state.clear()
    s = await _service(session, shop, data.get("service_id") or 0)
    try:
        start = datetime.fromisoformat(data.get("start") or "")
    except ValueError:
        start = None
    if s is None or start is None:
        await target.answer(tr("not_available"))
        return
    slots = await free_slots(session, barber.id, start.date(), s.duration, shop.slot_step)
    if start not in slots:
        await session.rollback()
        await target.answer(tr("slot_taken"))
        return
    left = start - now()
    session.add(
        Appointment(
            shop_id=shop.id,
            barber_id=barber.id,
            client_id=client.id,
            service_id=s.id,
            start_at=start,
            end_at=start + timedelta(minutes=s.duration),
            price=s.price,
            reminded_24=left <= timedelta(hours=24),
            reminded_2=left <= timedelta(hours=2),
        )
    )
    await session.commit()
    await target.answer(tr("manual_booked", client=h(client.name or "—"), when=fmt_dt(start, tr), service=h(s.name)))
    await notify_user(
        bot,
        client,
        "booked_by_barber",
        barber=h(barber.name),
        service=h(s.name),
        when=lambda t: fmt_dt(start, t),
        address=h(shop.address or "—"),
    )
    text, kb = await render_day(session, barber, start.date(), tr)
    await target.answer(text, reply_markup=kb)


# ======================= running late =======================


@router.message(F.text.in_(variants("btn_late")))
async def late_menu(message: Message, tr: Tr, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    for m in (10, 15, 20, 30):
        b.button(text=tr("minutes_short", m=m), callback_data=BarCB(act="late", id=m))
    b.adjust(4)
    await message.answer(tr("late_choose"), reply_markup=b.as_markup())


@router.callback_query(BarCB.filter(F.act == "late"))
async def late_send(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr, bot: Bot):
    current = now()
    end_of_day = datetime.combine(current.date(), time.max)
    appts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.barber_id == barber.id,
                Appointment.status.in_(ACTIVE),
                Appointment.start_at >= current - timedelta(minutes=10),
                Appointment.start_at <= end_of_day,
            )
            .order_by(Appointment.start_at)
        )
    ).all()
    sent = 0
    for a in appts:
        start = a.start_at
        if await notify_user(
            bot, a.client, "late_notice", barber=h(barber.name), minutes=callback_data.id, time=f"{start:%H:%M}"
        ):
            sent += 1
    await safe_edit(cb.message, tr("late_sent", count=sent, minutes=callback_data.id))
    await cb.answer()


# ======================= block time (break / day off) =======================


async def _block_days_view(session: AsyncSession, barber: Barber, tr: Tr):
    hours = await get_hours(session, barber.id)
    d0 = today()
    b = InlineKeyboardBuilder()
    for i in range(14):
        d = d0 + timedelta(days=i)
        if d.weekday() in hours:
            b.button(text=fmt_day(d, tr), callback_data=BarCB(act="blk_day", day=d.strftime("%Y%m%d")))
    b.adjust(3)
    b.row(InlineKeyboardButton(text=tr("btn_my_blocks"), callback_data=BarCB(act="blk_list").pack()))
    return tr("blk_choose_day"), b.as_markup()


@router.message(F.text.in_(variants("btn_block")))
async def block_menu(message: Message, session: AsyncSession, barber: Barber, tr: Tr, state: FSMContext):
    await state.clear()
    text, kb = await _block_days_view(session, barber, tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(BarCB.filter(F.act == "blk_menu"))
async def block_menu_cb(cb: CallbackQuery, session: AsyncSession, barber: Barber, tr: Tr):
    text, kb = await _block_days_view(session, barber, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "blk_day"))
async def block_day(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    day = parse_day(callback_data.day)
    wh = (await get_hours(session, barber.id)).get(day.weekday()) if day else None
    if wh is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_whole_day"), callback_data=BarCB(act="blk_make", day=callback_data.day, val="full"))
    t = datetime.combine(day, hm(wh.opens))
    closes = datetime.combine(day, hm(wh.closes))
    current = now()
    while t + timedelta(minutes=30) <= closes:
        if t + timedelta(minutes=30) > current:
            b.button(text=f"{t:%H:%M}", callback_data=BarCB(act="blk_start", day=callback_data.day, val=f"{t:%H%M}"))
        t += timedelta(minutes=30)
    b.adjust(1, 4)
    b.row(InlineKeyboardButton(text=tr("btn_back"), callback_data=BarCB(act="blk_menu").pack()))
    await safe_edit(cb.message, tr("blk_choose_start", day=fmt_day(day, tr)), b.as_markup())
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "blk_start"))
async def block_start(cb: CallbackQuery, callback_data: BarCB, tr: Tr):
    b = InlineKeyboardBuilder()
    for m in (30, 60, 90, 120):
        b.button(text=tr("minutes_short", m=m), callback_data=BarCB(act="blk_make", day=callback_data.day, val=f"{callback_data.val}-{m}"))
    b.button(text=tr("btn_until_close"), callback_data=BarCB(act="blk_make", day=callback_data.day, val=f"{callback_data.val}-end"))
    b.adjust(4, 1)
    b.row(InlineKeyboardButton(text=tr("btn_back"), callback_data=BarCB(act="blk_day", day=callback_data.day).pack()))
    await safe_edit(cb.message, tr("blk_choose_duration"), b.as_markup())
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "blk_make"))
async def block_make(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    day = parse_day(callback_data.day)
    wh = (await get_hours(session, barber.id)).get(day.weekday()) if day else None
    if wh is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    opens = datetime.combine(day, hm(wh.opens))
    closes = datetime.combine(day, hm(wh.closes))
    if callback_data.val == "full":
        start, end = opens, closes
    else:
        hhmm, _, dur = callback_data.val.partition("-")
        t = parse_hhmm(hhmm)
        if t is None:
            await cb.answer(tr("not_available"), show_alert=True)
            return
        start = datetime.combine(day, t)
        end = closes if dur == "end" else min(start + timedelta(minutes=int(dur or 30)), closes)
    if end <= start:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    session.add(Block(barber_id=barber.id, start_at=start, end_at=end))
    await session.commit()
    conflicts = (
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.barber_id == barber.id,
                Appointment.status.in_(ACTIVE),
                Appointment.start_at < end,
                Appointment.end_at > start,
            )
            .order_by(Appointment.start_at)
        )
    ).all()
    text = tr("blk_ok", day=fmt_day(day, tr), start=f"{start:%H:%M}", end=f"{end:%H:%M}")
    if conflicts:
        text += "\n\n" + tr("blk_conflicts", n=len(conflicts)) + "\n"
        text += "\n".join(f"• {a.start_at:%H:%M} {client_name(a, tr)}" for a in conflicts)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr("btn_my_blocks"), callback_data=BarCB(act="blk_list").pack())]]
    )
    await safe_edit(cb.message, text, kb)
    await cb.answer("✅")


async def _blocks_view(session: AsyncSession, barber: Barber, tr: Tr):
    blocks = (
        await session.scalars(
            select(Block).where(Block.barber_id == barber.id, Block.end_at > now()).order_by(Block.start_at).limit(20)
        )
    ).all()
    b = InlineKeyboardBuilder()
    for bl in blocks:
        b.button(
            text=f"🗑 {fmt_day(bl.start_at.date(), tr)} {bl.start_at:%H:%M}–{bl.end_at:%H:%M}",
            callback_data=BarCB(act="blk_del", id=bl.id),
        )
    b.button(text=tr("btn_back"), callback_data=BarCB(act="blk_menu"))
    b.adjust(1)
    return (tr("blk_list") if blocks else tr("blk_none")), b.as_markup()


@router.callback_query(BarCB.filter(F.act == "blk_list"))
async def block_list(cb: CallbackQuery, session: AsyncSession, barber: Barber, tr: Tr):
    text, kb = await _blocks_view(session, barber, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(BarCB.filter(F.act == "blk_del"))
async def block_delete(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    bl = await session.get(Block, callback_data.id)
    if bl is not None and bl.barber_id == barber.id:
        await session.delete(bl)
        await session.commit()
    text, kb = await _blocks_view(session, barber, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer(tr("blk_deleted"))


# ======================= earnings =======================


async def earnings_text(session: AsyncSession, barber: Barber, kind: str, tr: Tr) -> str:
    start, end = period_range(kind, today())
    st = await barber_stats(session, barber.id, start, end)
    share = st["revenue"] * barber.commission_pct // 100
    return tr(
        "earn_report",
        period=tr("period_" + kind),
        count=st["count"],
        revenue=money(st["revenue"], tr),
        pct=barber.commission_pct,
        share=money(share, tr),
        tips=money(st["tips"], tr),
        total=money(share + st["tips"], tr),
        noshows=st["no_shows"],
    )


def _earn_kb(tr: Tr) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for kind in ("today", "week", "month"):
        b.button(text=tr("period_" + kind), callback_data=BarCB(act="earn", val=kind))
    b.adjust(3)
    return b.as_markup()


@router.message(F.text.in_(variants("btn_earn")))
async def earnings(message: Message, session: AsyncSession, barber: Barber, tr: Tr, state: FSMContext):
    await state.clear()
    await message.answer(await earnings_text(session, barber, "today", tr), reply_markup=_earn_kb(tr))


@router.callback_query(BarCB.filter(F.act == "earn"))
async def earnings_cb(cb: CallbackQuery, callback_data: BarCB, session: AsyncSession, barber: Barber, tr: Tr):
    kind = callback_data.val if callback_data.val in ("today", "week", "month") else "today"
    await safe_edit(cb.message, await earnings_text(session, barber, kind, tr), _earn_kb(tr))
    await cb.answer()


# ======================= shift check-in / check-out =======================


@router.message(F.text.in_(variants("btn_shift")))
async def shift_toggle(message: Message, session: AsyncSession, barber: Barber, tr: Tr, state: FSMContext):
    await state.clear()
    current = now()
    open_shift = await session.scalar(
        select(Shift).where(Shift.barber_id == barber.id, Shift.end_at.is_(None)).order_by(Shift.start_at.desc()).limit(1)
    )
    if open_shift is not None:
        open_shift.end_at = current
        await session.commit()
        minutes = int((current - open_shift.start_at).total_seconds() // 60)
        await message.answer(tr("shift_ended", hours=minutes // 60, minutes=minutes % 60))
    else:
        session.add(Shift(barber_id=barber.id, start_at=current))
        await session.commit()
        await message.answer(tr("shift_started", time=f"{current:%H:%M}"))
