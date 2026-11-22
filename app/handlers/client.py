"""Client side: booking flow, my bookings, cancel/confirm, contacts."""

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..keyboards import BookCB, CliCB, book_btn
from ..models import Appointment, Barber, Service, Shop, User
from ..notify import notify_tg
from ..services.slots import ACTIVE, available_days, free_slots
from ..texts import Tr, variants
from ..timeutil import now
from ..utils import fmt_day, fmt_dt, h, money, parse_day, parse_hhmm, safe_edit
from ..views import active_services, barbers_view

router = Router(name="client")

MAX_UPCOMING = 3  # anti-spam: max future bookings per client


def _guard(user: User | None, shop: Shop, tr: Tr) -> str | None:
    if user is None or not user.phone or not user.name:
        return tr("press_start")
    if user.blocked:
        return tr("client_blocked", phone=h(shop.phone or "—"))
    return None


async def _barber(session: AsyncSession, shop: Shop, barber_id: int) -> Barber | None:
    b = await session.get(Barber, barber_id)
    return b if b and b.shop_id == shop.id and b.active else None


async def _service(session: AsyncSession, shop: Shop, service_id: int) -> Service | None:
    s = await session.get(Service, service_id)
    return s if s and s.shop_id == shop.id and s.active else None


def _start_dt(cbd: BookCB) -> datetime | None:
    d, t = parse_day(cbd.day), parse_hhmm(cbd.time)
    return datetime.combine(d, t) if d and t else None


# ---------- step 1: barbers ----------


@router.message(F.text.in_(variants("btn_book")))
async def book_msg(message: Message, session: AsyncSession, shop: Shop, user: User | None, tr: Tr, state: FSMContext):
    await state.clear()
    err = _guard(user, shop, tr)
    if err:
        await message.answer(err)
        return
    text, kb = await barbers_view(session, shop, tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(BookCB.filter(F.act.in_({"start", "list"})))
async def book_list(cb: CallbackQuery, callback_data: BookCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr):
    err = _guard(user, shop, tr)
    if err:
        await cb.answer(err[:190], show_alert=True)
        return
    text, kb = await barbers_view(session, shop, tr)
    if callback_data.act == "start":
        await cb.message.answer(text, reply_markup=kb)
    else:
        await safe_edit(cb.message, text, kb)
    await cb.answer()


# ---------- step 2: services ----------


@router.callback_query(BookCB.filter(F.act == "b"))
async def book_barber(cb: CallbackQuery, callback_data: BookCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr):
    err = _guard(user, shop, tr)
    if err:
        await cb.answer(err[:190], show_alert=True)
        return
    barber = await _barber(session, shop, callback_data.barber)
    if barber is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    services = await active_services(session, shop.id)
    b = InlineKeyboardBuilder()
    for s in services:
        b.button(
            text=f"{s.name} · {money(s.price, tr)} · {s.duration} {tr('min')}",
            callback_data=BookCB(act="s", barber=barber.id, service=s.id),
        )
    b.button(text=tr("btn_back"), callback_data=BookCB(act="list"))
    b.adjust(1)
    text = tr("choose_service", barber=h(barber.name)) if services else tr("no_services")
    await safe_edit(cb.message, text, b.as_markup())
    await cb.answer()


# ---------- step 3: day ----------


@router.callback_query(BookCB.filter(F.act == "s"))
async def book_service(cb: CallbackQuery, callback_data: BookCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr):
    err = _guard(user, shop, tr)
    if err:
        await cb.answer(err[:190], show_alert=True)
        return
    barber = await _barber(session, shop, callback_data.barber)
    service = await _service(session, shop, callback_data.service)
    if barber is None or service is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    days = await available_days(session, barber.id, service.duration, shop.slot_step)
    b = InlineKeyboardBuilder()
    for d in days:
        b.button(
            text=fmt_day(d, tr),
            callback_data=BookCB(act="d", barber=barber.id, service=service.id, day=d.strftime("%Y%m%d")),
        )
    b.adjust(3)
    b.row(InlineKeyboardButton(text=tr("btn_back"), callback_data=BookCB(act="b", barber=barber.id).pack()))
    text = tr("choose_day", barber=h(barber.name), service=h(service.name)) if days else tr("no_days")
    await safe_edit(cb.message, text, b.as_markup())
    await cb.answer()


# ---------- step 4: time ----------


@router.callback_query(BookCB.filter(F.act == "d"))
async def book_day(cb: CallbackQuery, callback_data: BookCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr):
    err = _guard(user, shop, tr)
    if err:
        await cb.answer(err[:190], show_alert=True)
        return
    barber = await _barber(session, shop, callback_data.barber)
    service = await _service(session, shop, callback_data.service)
    day = parse_day(callback_data.day)
    if barber is None or service is None or day is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    slots = await free_slots(session, barber.id, day, service.duration, shop.slot_step)
    b = InlineKeyboardBuilder()
    for t in slots:
        b.button(
            text=f"{t:%H:%M}",
            callback_data=BookCB(
                act="t", barber=barber.id, service=service.id, day=callback_data.day, time=f"{t:%H%M}"
            ),
        )
    b.adjust(4)
    b.row(
        InlineKeyboardButton(
            text=tr("btn_back"), callback_data=BookCB(act="s", barber=barber.id, service=service.id).pack()
        )
    )
    text = tr("choose_time", day=fmt_day(day, tr)) if slots else tr("no_slots")
    await safe_edit(cb.message, text, b.as_markup())
    await cb.answer()


# ---------- step 5: confirm ----------


@router.callback_query(BookCB.filter(F.act == "t"))
async def book_time(cb: CallbackQuery, callback_data: BookCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr):
    err = _guard(user, shop, tr)
    if err:
        await cb.answer(err[:190], show_alert=True)
        return
    barber = await _barber(session, shop, callback_data.barber)
    service = await _service(session, shop, callback_data.service)
    start = _start_dt(callback_data)
    if barber is None or service is None or start is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    text = tr(
        "confirm_booking",
        barber=h(barber.name),
        service=h(service.name),
        when=fmt_dt(start, tr),
        duration=service.duration,
        price=money(service.price, tr),
        address=h(shop.address or "—"),
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr("btn_confirm"),
                    callback_data=BookCB(
                        act="ok", barber=barber.id, service=service.id, day=callback_data.day, time=callback_data.time
                    ).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=tr("btn_back"),
                    callback_data=BookCB(act="d", barber=barber.id, service=service.id, day=callback_data.day).pack(),
                )
            ],
        ]
    )
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(BookCB.filter(F.act == "ok"))
async def book_ok(
    cb: CallbackQuery, callback_data: BookCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr, bot: Bot
):
    err = _guard(user, shop, tr)
    if err:
        await cb.answer(err[:190], show_alert=True)
        return
    barber = await _barber(session, shop, callback_data.barber)
    service = await _service(session, shop, callback_data.service)
    start = _start_dt(callback_data)
    if barber is None or service is None or start is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return

    # Re-check everything: the slot may have been taken while the client was choosing.
    slots = await free_slots(session, barber.id, start.date(), service.duration, shop.slot_step)
    if start not in slots:
        await cb.answer(tr("slot_taken"), show_alert=True)
        return
    current = now()
    end = start + timedelta(minutes=service.duration)
    upcoming = await session.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(Appointment.client_id == user.id, Appointment.status.in_(ACTIVE), Appointment.start_at > current)
    )
    if (upcoming or 0) >= MAX_UPCOMING:
        await cb.answer(tr("too_many_bookings"), show_alert=True)
        return
    overlap = await session.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            Appointment.client_id == user.id,
            Appointment.status.in_(ACTIVE),
            Appointment.start_at < end,
            Appointment.end_at > start,
        )
    )
    if overlap:
        await cb.answer(tr("you_are_busy"), show_alert=True)
        return

    left = start - current
    appt = Appointment(
        shop_id=shop.id,
        barber_id=barber.id,
        client_id=user.id,
        service_id=service.id,
        start_at=start,
        end_at=end,
        price=service.price,
        reminded_24=left <= timedelta(hours=24),
        reminded_2=left <= timedelta(hours=2),
    )
    session.add(appt)
    await session.commit()

    await safe_edit(
        cb.message,
        tr(
            "booked_ok",
            barber=h(barber.name),
            service=h(service.name),
            when=fmt_dt(start, tr),
            address=h(shop.address or "—"),
        ),
    )
    await cb.answer("✅")
    await notify_tg(
        bot,
        session,
        shop,
        barber.tg_id,
        "new_booking",
        client=h(user.name),
        phone=h(user.phone),
        service=h(service.name),
        when=lambda t: fmt_dt(start, t),
    )


# ---------- my bookings ----------


@router.message(F.text.in_(variants("btn_my")))
async def my_bookings(message: Message, session: AsyncSession, shop: Shop, user: User | None, tr: Tr, state: FSMContext):
    await state.clear()
    if user is None or not user.phone:
        await message.answer(tr("press_start"))
        return
    appts = (
        await session.scalars(
            select(Appointment)
            .where(Appointment.client_id == user.id, Appointment.status.in_(ACTIVE), Appointment.end_at > now())
            .order_by(Appointment.start_at)
        )
    ).all()
    if not appts:
        await message.answer(tr("no_bookings"), reply_markup=book_btn(tr))
        return
    lines = [tr("my_bookings_title")]
    b = InlineKeyboardBuilder()
    for a in appts:
        lines.append(
            tr(
                "my_booking_line",
                when=fmt_dt(a.start_at, tr),
                barber=h(a.barber.name),
                service=h(a.service.name),
                status=tr("st_" + a.status),
            )
        )
        b.button(text=tr("btn_cancel_booking", when=fmt_dt(a.start_at, tr)), callback_data=CliCB(act="ask", id=a.id))
    b.adjust(1)
    await message.answer("\n\n".join(lines), reply_markup=b.as_markup())


async def _own(session: AsyncSession, user: User | None, appt_id: int) -> Appointment | None:
    a = await session.get(Appointment, appt_id)
    return a if a and user and a.client_id == user.id else None


@router.callback_query(CliCB.filter(F.act == "ask"))
async def cancel_ask(cb: CallbackQuery, callback_data: CliCB, session: AsyncSession, user: User | None, tr: Tr):
    a = await _own(session, user, callback_data.id)
    if a is None or a.status not in ACTIVE or a.start_at <= now():
        await cb.answer(tr("not_available"), show_alert=True)
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tr("btn_yes_cancel"), callback_data=CliCB(act="yes", id=a.id).pack()),
                InlineKeyboardButton(text=tr("btn_no_keep"), callback_data=CliCB(act="no", id=a.id).pack()),
            ]
        ]
    )
    await safe_edit(cb.message, tr("cancel_confirm", when=fmt_dt(a.start_at, tr), barber=h(a.barber.name)), kb)
    await cb.answer()


@router.callback_query(CliCB.filter(F.act == "yes"))
async def cancel_yes(
    cb: CallbackQuery, callback_data: CliCB, session: AsyncSession, shop: Shop, user: User | None, tr: Tr, bot: Bot
):
    a = await _own(session, user, callback_data.id)
    if a is None or a.status not in ACTIVE or a.start_at <= now():
        await cb.answer(tr("not_available"), show_alert=True)
        return
    a.status = "cancelled"
    await session.commit()
    await safe_edit(cb.message, tr("cancelled_ok", when=fmt_dt(a.start_at, tr)), book_btn(tr))
    await cb.answer()
    start = a.start_at
    await notify_tg(
        bot,
        session,
        shop,
        a.barber.tg_id,
        "client_cancelled",
        client=h(user.name),
        service=h(a.service.name),
        when=lambda t: fmt_dt(start, t),
    )


@router.callback_query(CliCB.filter(F.act == "no"))
async def cancel_no(cb: CallbackQuery, callback_data: CliCB, session: AsyncSession, user: User | None, tr: Tr):
    a = await _own(session, user, callback_data.id)
    when = fmt_dt(a.start_at, tr) if a else ""
    await safe_edit(cb.message, tr("cancel_kept", when=when))
    await cb.answer()


@router.callback_query(CliCB.filter(F.act == "confirm"))
async def confirm_visit(cb: CallbackQuery, callback_data: CliCB, session: AsyncSession, user: User | None, tr: Tr):
    a = await _own(session, user, callback_data.id)
    if a is None or a.status not in ACTIVE:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    a.status = "confirmed"
    await session.commit()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=tr("btn_cancel_visit"), callback_data=CliCB(act="ask", id=a.id).pack())]]
    )
    await safe_edit(cb.message, tr("confirmed_ok", when=fmt_dt(a.start_at, tr), barber=h(a.barber.name)), kb)
    await cb.answer("👍")


@router.callback_query(CliCB.filter(F.act == "later"))
async def nudge_later(cb: CallbackQuery, tr: Tr):
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001
        pass
    await cb.answer(tr("ok_later"))


# ---------- contacts ----------


@router.message(F.text.in_(variants("btn_contacts")))
async def contacts(message: Message, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    await message.answer(
        tr(
            "contacts",
            name=h(shop.name),
            address=h(shop.address or "—"),
            phone=h(shop.phone or "—"),
            hours=h(shop.hours_text or "—"),
        )
    )
    if shop.latitude is not None and shop.longitude is not None:
        await message.answer_location(latitude=shop.latitude, longitude=shop.longitude)
