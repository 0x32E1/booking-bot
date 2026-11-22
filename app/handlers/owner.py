"""Owner panel: dashboard, barbers (+hours, commission, invite), services, salary,
analytics, clients, fill free slots, shop settings."""

import asyncio
import secrets
from datetime import datetime, time, timedelta

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import registry
from ..filters import RoleFilter
from ..keyboards import FSM_CANCEL, MENU_TEXTS, BookCB, CardCB, OwCB, cancel_kb
from ..models import Appointment, Barber, Service, Shift, Shop, User, WorkHours
from ..services.reports import barber_stats, period_range, shift_minutes, shop_analytics
from ..services.slots import ACTIVE, free_slots, get_hours
from ..texts import WEEKDAYS, WEEKDAYS_FULL, Tr, variants
from ..timeutil import now, today
from ..utils import fmt_day, h, money, normalize_phone, parse_hours, parse_int, safe_edit, safe_send
from ..views import active_services, bookable_barbers

router = Router(name="owner")
router.message.filter(RoleFilter("owner"))
router.callback_query.filter(RoleFilter("owner"))

DEFAULT_HOURS = ("09:00", "20:00")
DEFAULT_WEEKDAYS = range(0, 6)  # Mon–Sat


class OwnerStates(StatesGroup):
    barber_name = State()
    barber_pct = State()
    barber_edit = State()
    hours = State()
    svc_name = State()
    svc_duration = State()
    svc_price = State()
    svc_edit = State()
    setting = State()
    client_search = State()


def _new_code() -> str:
    return secrets.token_urlsafe(6)


async def _barber(session: AsyncSession, shop: Shop, barber_id: int) -> Barber | None:
    b = await session.get(Barber, barber_id)
    return b if b and b.shop_id == shop.id else None


async def _service(session: AsyncSession, shop: Shop, service_id: int) -> Service | None:
    s = await session.get(Service, service_id)
    return s if s and s.shop_id == shop.id and s.active else None


# ======================= dashboard =======================


async def dashboard_text(session: AsyncSession, shop: Shop, tr: Tr) -> str:
    current = now()
    start = datetime.combine(current.date(), time.min)
    end = start + timedelta(days=1)
    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.shop_id == shop.id, Appointment.start_at >= start, Appointment.start_at < end
            )
        )
    ).all()
    live = [a for a in appts if a.status != "cancelled"]
    done = [a for a in live if a.status == "done"]
    upcoming = [a for a in live if a.status in ACTIVE and a.start_at >= current]
    unmarked = [a for a in live if a.status in ACTIVE and a.end_at < current]
    no_shows = [a for a in live if a.status == "no_show"]

    lines = [
        tr("dash_title", day=fmt_day(current.date(), tr)),
        tr(
            "dash_stats",
            total=len(live),
            done=len(done),
            upcoming=len(upcoming),
            noshow=len(no_shows),
            cancelled=len(appts) - len(live),
        ),
        tr(
            "dash_money",
            revenue=money(sum(a.price or 0 for a in done), tr),
            tips=money(sum(a.tip or 0 for a in done), tr),
        ),
        "",
    ]
    barbers = (
        await session.scalars(select(Barber).where(Barber.shop_id == shop.id, Barber.active.is_(True)).order_by(Barber.id))
    ).all()
    on_shift: set[int] = set()
    if barbers:
        on_shift = set(
            (
                await session.scalars(
                    select(Shift.barber_id).where(Shift.end_at.is_(None), Shift.barber_id.in_([b.id for b in barbers]))
                )
            ).all()
        )
    for br in barbers:
        mine = [a for a in live if a.barber_id == br.id]
        nxt = min((a.start_at for a in mine if a.status in ACTIVE and a.start_at >= current), default=None)
        lines.append(
            tr(
                "dash_barber",
                icon="🟢" if br.id in on_shift else "⚪",
                name=h(br.name),
                done=sum(1 for a in mine if a.status == "done"),
                total=len(mine),
                next=f"{nxt:%H:%M}" if nxt else "—",
            )
        )
    if not barbers:
        lines.append(tr("dash_no_barbers"))
    if unmarked:
        lines += ["", tr("dash_unmarked", n=len(unmarked))]
    lines += ["", tr("dash_footer", paid_until=shop.paid_until.strftime("%d.%m.%Y"), link=registry.client_link(shop.id))]
    return "\n".join(lines)


def _refresh_kb(act: str, tr: Tr) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=tr("btn_refresh"), callback_data=OwCB(act=act).pack())]])


@router.message(F.text.in_(variants("btn_dash")))
async def dashboard(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    await message.answer(await dashboard_text(session, shop, tr), reply_markup=_refresh_kb("dash", tr), disable_web_page_preview=True)


@router.callback_query(OwCB.filter(F.act == "dash"))
async def dashboard_cb(cb: CallbackQuery, session: AsyncSession, shop: Shop, tr: Tr):
    await safe_edit(cb.message, await dashboard_text(session, shop, tr), _refresh_kb("dash", tr), disable_web_page_preview=True)
    await cb.answer()


# ======================= barbers =======================


async def barbers_view(session: AsyncSession, shop: Shop, tr: Tr):
    barbers = (await session.scalars(select(Barber).where(Barber.shop_id == shop.id).order_by(Barber.id))).all()
    b = InlineKeyboardBuilder()
    for br in barbers:
        b.button(text=f"{'💈' if br.active else '⏸'} {br.name}", callback_data=OwCB(act="barber", id=br.id))
    b.button(text=tr("btn_add_barber"), callback_data=OwCB(act="barber_add"))
    b.adjust(1)
    return (tr("barbers_title") if barbers else tr("barbers_empty")), b.as_markup()


async def barber_view(session: AsyncSession, shop: Shop, barber: Barber, tr: Tr):
    hours = await get_hours(session, barber.id)
    lines = [
        tr(
            "barber_detail",
            name=h(barber.name),
            pct=barber.commission_pct,
            status=tr("active") if barber.active else tr("inactive"),
            linked=tr("tg_linked") if barber.tg_id else tr("tg_not_linked"),
        ),
        "",
        tr("hours_title_short"),
    ]
    for wd in range(7):
        wh = hours.get(wd)
        lines.append(f"{WEEKDAYS[tr.lang][wd]}: " + (f"{wh.opens}–{wh.closes}" if wh else tr("day_off_short")))
    if not barber.tg_id:
        lines += ["", tr("invite_hint", link=registry.invite_link(shop.id, barber.invite_code))]
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_hours"), callback_data=OwCB(act="hours", id=barber.id))
    b.button(text=tr("btn_commission"), callback_data=OwCB(act="b_edit", id=barber.id, val="pct"))
    b.button(text=tr("btn_rename"), callback_data=OwCB(act="b_edit", id=barber.id, val="name"))
    if barber.tg_id:
        b.button(text=tr("btn_unlink"), callback_data=OwCB(act="b_unlink", id=barber.id))
    b.button(text=tr("btn_deactivate") if barber.active else tr("btn_activate"), callback_data=OwCB(act="b_toggle", id=barber.id))
    b.button(text=tr("btn_back"), callback_data=OwCB(act="barbers"))
    b.adjust(2)
    return "\n".join(lines), b.as_markup()


@router.message(F.text.in_(variants("btn_barbers")))
async def barbers_list(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    text, kb = await barbers_view(session, shop, tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(OwCB.filter(F.act == "barbers"))
async def barbers_list_cb(cb: CallbackQuery, session: AsyncSession, shop: Shop, tr: Tr):
    text, kb = await barbers_view(session, shop, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(OwCB.filter(F.act == "barber"))
async def barber_detail(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    if barber is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    text, kb = await barber_view(session, shop, barber, tr)
    await safe_edit(cb.message, text, kb, disable_web_page_preview=True)
    await cb.answer()


@router.callback_query(OwCB.filter(F.act == "barber_add"))
async def barber_add(cb: CallbackQuery, state: FSMContext, tr: Tr):
    await state.set_state(OwnerStates.barber_name)
    await cb.message.answer(tr("ask_barber_name"), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(OwnerStates.barber_name, F.text, ~F.text.in_(MENU_TEXTS))
async def barber_add_name(message: Message, state: FSMContext, tr: Tr):
    name = " ".join(message.text.split())
    if len(name) < 2 or len(name) > 60:
        await message.answer(tr("name_invalid"))
        return
    await state.update_data(name=name)
    await state.set_state(OwnerStates.barber_pct)
    await message.answer(tr("ask_barber_pct"), reply_markup=cancel_kb(tr))


@router.message(OwnerStates.barber_pct, F.text, ~F.text.in_(MENU_TEXTS))
async def barber_add_pct(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    pct = parse_int(message.text.replace("%", ""))
    if pct is None or pct > 100:
        await message.answer(tr("pct_invalid"))
        return
    data = await state.get_data()
    await state.clear()
    barber = Barber(shop_id=shop.id, name=data["name"], commission_pct=pct, invite_code=_new_code())
    session.add(barber)
    await session.flush()
    for wd in DEFAULT_WEEKDAYS:
        session.add(WorkHours(barber_id=barber.id, weekday=wd, opens=DEFAULT_HOURS[0], closes=DEFAULT_HOURS[1]))
    await session.commit()
    await message.answer(
        tr("barber_added", name=h(barber.name), link=registry.invite_link(shop.id, barber.invite_code)),
        disable_web_page_preview=True,
    )
    text, kb = await barber_view(session, shop, barber, tr)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(OwCB.filter(F.act == "b_edit"))
async def barber_edit_start(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    if barber is None or callback_data.val not in ("pct", "name"):
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(OwnerStates.barber_edit)
    await state.update_data(barber_id=barber.id, field=callback_data.val)
    await cb.message.answer(tr("ask_barber_pct") if callback_data.val == "pct" else tr("ask_barber_name"), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(OwnerStates.barber_edit, F.text, ~F.text.in_(MENU_TEXTS))
async def barber_edit_save(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    data = await state.get_data()
    barber = await _barber(session, shop, data.get("barber_id") or 0)
    if barber is None:
        await state.clear()
        await message.answer(tr("not_available"))
        return
    if data.get("field") == "pct":
        pct = parse_int(message.text.replace("%", ""))
        if pct is None or pct > 100:
            await message.answer(tr("pct_invalid"))
            return
        barber.commission_pct = pct
    else:
        name = " ".join(message.text.split())
        if len(name) < 2 or len(name) > 60:
            await message.answer(tr("name_invalid"))
            return
        barber.name = name
    await state.clear()
    await session.commit()
    await message.answer(tr("saved"))
    text, kb = await barber_view(session, shop, barber, tr)
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(OwCB.filter(F.act == "b_unlink"))
async def barber_unlink(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    if barber is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    barber.tg_id = None
    barber.invite_code = _new_code()  # old link stops working
    await session.commit()
    text, kb = await barber_view(session, shop, barber, tr)
    await safe_edit(cb.message, text, kb, disable_web_page_preview=True)
    await cb.answer(tr("unlinked"), show_alert=True)


@router.callback_query(OwCB.filter(F.act == "b_toggle"))
async def barber_toggle(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    if barber is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    barber.active = not barber.active
    await session.commit()
    note = ""
    if not barber.active:
        future = await session.scalar(
            select(func.count())
            .select_from(Appointment)
            .where(Appointment.barber_id == barber.id, Appointment.status.in_(ACTIVE), Appointment.start_at > now())
        )
        if future:
            note = tr("deactivated_has_bookings", n=future)
    text, kb = await barber_view(session, shop, barber, tr)
    await safe_edit(cb.message, text, kb, disable_web_page_preview=True)
    await cb.answer(note or tr("saved"), show_alert=bool(note))


# ---------- working hours ----------


async def hours_view(session: AsyncSession, barber: Barber, tr: Tr):
    hours = await get_hours(session, barber.id)
    b = InlineKeyboardBuilder()
    for wd in range(7):
        wh = hours.get(wd)
        label = f"{WEEKDAYS_FULL[tr.lang][wd]}: " + (f"{wh.opens}–{wh.closes}" if wh else tr("day_off_short"))
        b.button(text=label, callback_data=OwCB(act="hours_day", id=barber.id, val=str(wd)))
    b.button(text=tr("btn_back"), callback_data=OwCB(act="barber", id=barber.id))
    b.adjust(1)
    return tr("hours_title", name=h(barber.name)), b.as_markup()


@router.callback_query(OwCB.filter(F.act == "hours"))
async def hours_open(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    if barber is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    text, kb = await hours_view(session, barber, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(OwCB.filter(F.act == "hours_day"))
async def hours_day(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    wd = int(callback_data.val) if callback_data.val.isdigit() else -1
    if barber is None or not 0 <= wd <= 6:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(OwnerStates.hours)
    await state.update_data(barber_id=barber.id, weekday=wd)
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_day_off"), callback_data=OwCB(act="hours_off", id=barber.id, val=str(wd)))
    b.button(text=tr("btn_all_days"), callback_data=OwCB(act="hours_all", id=barber.id, val=str(wd)))
    b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
    b.adjust(1)
    await cb.message.answer(tr("ask_hours", day=WEEKDAYS_FULL[tr.lang][wd]), reply_markup=b.as_markup())
    await cb.answer()


@router.callback_query(OwCB.filter(F.act == "hours_off"))
async def hours_off(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    barber = await _barber(session, shop, callback_data.id)
    if barber is None or not callback_data.val.isdigit():
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.clear()
    wh = (await get_hours(session, barber.id)).get(int(callback_data.val))
    if wh is not None:
        await session.delete(wh)
        await session.commit()
    text, kb = await hours_view(session, barber, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer(tr("saved"))


@router.callback_query(OwnerStates.hours, OwCB.filter(F.act == "hours_all"))
async def hours_all(cb: CallbackQuery, state: FSMContext, tr: Tr):
    await state.update_data(apply_all=True)
    await cb.message.answer(tr("ask_hours_all"), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(OwnerStates.hours, F.text, ~F.text.in_(MENU_TEXTS))
async def hours_save(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    parsed = parse_hours(message.text)
    if parsed is None:
        await message.answer(tr("hours_invalid"))
        return
    data = await state.get_data()
    barber = await _barber(session, shop, data.get("barber_id") or 0)
    await state.clear()
    if barber is None:
        await message.answer(tr("not_available"))
        return
    hours = await get_hours(session, barber.id)
    days = [wd for wd in range(7) if wd in hours] if data.get("apply_all") else [data["weekday"]]
    if data.get("apply_all") and data["weekday"] not in days:
        days.append(data["weekday"])
    for wd in days:
        wh = hours.get(wd)
        if wh is None:
            session.add(WorkHours(barber_id=barber.id, weekday=wd, opens=parsed[0], closes=parsed[1]))
        else:
            wh.opens, wh.closes = parsed
    await session.commit()
    text, kb = await hours_view(session, barber, tr)
    await message.answer(text, reply_markup=kb)


# ======================= services =======================


async def services_view(session: AsyncSession, shop: Shop, tr: Tr):
    services = await active_services(session, shop.id)
    b = InlineKeyboardBuilder()
    for s in services:
        b.button(text=f"✂️ {s.name} · {money(s.price, tr)} · {s.duration} {tr('min')}", callback_data=OwCB(act="svc", id=s.id))
    b.button(text=tr("btn_add_service"), callback_data=OwCB(act="svc_add"))
    b.adjust(1)
    return (tr("services_title") if services else tr("services_empty")), b.as_markup()


async def service_view(service: Service, tr: Tr):
    text = tr("service_detail", name=h(service.name), price=money(service.price, tr), duration=service.duration)
    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_price"), callback_data=OwCB(act="svc_edit", id=service.id, val="price"))
    b.button(text=tr("btn_duration"), callback_data=OwCB(act="svc_edit", id=service.id, val="duration"))
    b.button(text=tr("btn_rename"), callback_data=OwCB(act="svc_edit", id=service.id, val="name"))
    b.button(text=tr("btn_delete"), callback_data=OwCB(act="svc_del", id=service.id))
    b.button(text=tr("btn_back"), callback_data=OwCB(act="services"))
    b.adjust(2)
    return text, b.as_markup()


@router.message(F.text.in_(variants("btn_services")))
async def services_list(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    text, kb = await services_view(session, shop, tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(OwCB.filter(F.act == "services"))
async def services_list_cb(cb: CallbackQuery, session: AsyncSession, shop: Shop, tr: Tr):
    text, kb = await services_view(session, shop, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(OwCB.filter(F.act == "svc"))
async def service_detail(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    s = await _service(session, shop, callback_data.id)
    if s is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    text, kb = await service_view(s, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer()


@router.callback_query(OwCB.filter(F.act == "svc_del"))
async def service_delete(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    s = await _service(session, shop, callback_data.id)
    if s is not None:
        s.active = False  # soft delete keeps history in reports
        await session.commit()
    text, kb = await services_view(session, shop, tr)
    await safe_edit(cb.message, text, kb)
    await cb.answer(tr("deleted"))


def _duration_kb(tr: Tr) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in (15, 20, 30, 45, 60, 90, 120):
        b.button(text=tr("minutes_short", m=m), callback_data=OwCB(act="svc_dur", val=str(m)))
    b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
    b.adjust(4, 3, 1)
    return b.as_markup()


@router.callback_query(OwCB.filter(F.act == "svc_add"))
async def service_add(cb: CallbackQuery, state: FSMContext, tr: Tr):
    await state.set_state(OwnerStates.svc_name)
    await cb.message.answer(tr("ask_service_name"), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(OwnerStates.svc_name, F.text, ~F.text.in_(MENU_TEXTS))
async def service_add_name(message: Message, state: FSMContext, tr: Tr):
    name = " ".join(message.text.split())
    if len(name) < 2 or len(name) > 60:
        await message.answer(tr("name_invalid"))
        return
    await state.update_data(name=name)
    await state.set_state(OwnerStates.svc_duration)
    await message.answer(tr("ask_service_duration"), reply_markup=_duration_kb(tr))


async def _service_set_duration(target: Message, state: FSMContext, minutes: int, tr: Tr):
    await state.update_data(duration=minutes)
    await state.set_state(OwnerStates.svc_price)
    await target.answer(tr("ask_service_price"), reply_markup=cancel_kb(tr))


@router.callback_query(OwnerStates.svc_duration, OwCB.filter(F.act == "svc_dur"))
async def service_add_duration_cb(cb: CallbackQuery, callback_data: OwCB, state: FSMContext, tr: Tr):
    await _service_set_duration(cb.message, state, int(callback_data.val), tr)
    await cb.answer()


@router.message(OwnerStates.svc_duration, F.text, ~F.text.in_(MENU_TEXTS))
async def service_add_duration_text(message: Message, state: FSMContext, tr: Tr):
    m = parse_int(message.text)
    if m is None or not 5 <= m <= 480:
        await message.answer(tr("duration_invalid"))
        return
    await _service_set_duration(message, state, m, tr)


@router.message(OwnerStates.svc_price, F.text, ~F.text.in_(MENU_TEXTS))
async def service_add_price(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    price = parse_int(message.text)
    if price is None or price > 100_000_000:
        await message.answer(tr("number_invalid"))
        return
    data = await state.get_data()
    await state.clear()
    session.add(Service(shop_id=shop.id, name=data["name"], duration=int(data["duration"]), price=price))
    await session.commit()
    await message.answer(tr("service_added", name=h(data["name"])))
    text, kb = await services_view(session, shop, tr)
    await message.answer(text, reply_markup=kb)


@router.callback_query(OwCB.filter(F.act == "svc_edit"))
async def service_edit_start(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    s = await _service(session, shop, callback_data.id)
    if s is None or callback_data.val not in ("price", "duration", "name"):
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(OwnerStates.svc_edit)
    await state.update_data(service_id=s.id, field=callback_data.val)
    prompt = {"price": "ask_service_price", "duration": "ask_service_duration_text", "name": "ask_service_name"}[callback_data.val]
    await cb.message.answer(tr(prompt), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(OwnerStates.svc_edit, F.text, ~F.text.in_(MENU_TEXTS))
async def service_edit_save(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    data = await state.get_data()
    s = await _service(session, shop, data.get("service_id") or 0)
    if s is None:
        await state.clear()
        await message.answer(tr("not_available"))
        return
    field = data.get("field")
    if field == "price":
        v = parse_int(message.text)
        if v is None or v > 100_000_000:
            await message.answer(tr("number_invalid"))
            return
        s.price = v
    elif field == "duration":
        v = parse_int(message.text)
        if v is None or not 5 <= v <= 480:
            await message.answer(tr("duration_invalid"))
            return
        s.duration = v
    else:
        name = " ".join(message.text.split())
        if len(name) < 2 or len(name) > 60:
            await message.answer(tr("name_invalid"))
            return
        s.name = name
    await state.clear()
    await session.commit()
    text, kb = await service_view(s, tr)
    await message.answer(tr("saved") + "\n\n" + text, reply_markup=kb)


# ======================= salary report =======================


async def salary_text(session: AsyncSession, shop: Shop, kind: str, tr: Tr) -> str:
    start, end = period_range(kind, today())
    barbers = (await session.scalars(select(Barber).where(Barber.shop_id == shop.id).order_by(Barber.id))).all()
    lines = [
        tr(
            "salary_title",
            period=tr("period_" + kind),
            start=start.strftime("%d.%m"),
            end=(end - timedelta(days=1)).strftime("%d.%m"),
        ),
        "",
    ]
    tot_rev = tot_share = tot_tips = 0
    for br in barbers:
        st = await barber_stats(session, br.id, start, end)
        if not br.active and st["count"] == 0:
            continue
        mins = await shift_minutes(session, br.id, start, end)
        share = st["revenue"] * br.commission_pct // 100
        lines.append(
            tr(
                "salary_line",
                name=h(br.name),
                count=st["count"],
                revenue=money(st["revenue"], tr),
                pct=br.commission_pct,
                share=money(share, tr),
                tips=money(st["tips"], tr),
                payout=money(share + st["tips"], tr),
                hours=f"{mins // 60}:{mins % 60:02d}",
                noshows=st["no_shows"],
            )
        )
        tot_rev += st["revenue"]
        tot_share += share
        tot_tips += st["tips"]
    lines.append(
        tr(
            "salary_total",
            revenue=money(tot_rev, tr),
            payout=money(tot_share + tot_tips, tr),
            shop_share=money(tot_rev - tot_share, tr),
        )
    )
    return "\n".join(lines)


def _salary_kb(tr: Tr) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for kind in ("week", "month", "last_month"):
        b.button(text=tr("period_" + kind), callback_data=OwCB(act="salary", val=kind))
    b.adjust(3)
    return b.as_markup()


@router.message(F.text.in_(variants("btn_salary")))
async def salary(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    await message.answer(await salary_text(session, shop, "month", tr), reply_markup=_salary_kb(tr))


@router.callback_query(OwCB.filter(F.act == "salary"))
async def salary_cb(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, tr: Tr):
    kind = callback_data.val if callback_data.val in ("week", "month", "last_month") else "month"
    await safe_edit(cb.message, await salary_text(session, shop, kind, tr), _salary_kb(tr))
    await cb.answer()


# ======================= analytics =======================


@router.message(F.text.in_(variants("btn_stats")))
async def analytics(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    a = await shop_analytics(session, shop.id)
    lines = [
        tr(
            "stats_main",
            done=a["done"],
            revenue=money(a["revenue"], tr),
            avg=money(a["avg_check"], tr),
            walkins=a["walkins"],
            noshow_rate=a["no_show_rate"],
            cancelled=a["cancelled"],
            new_clients=a["new_clients"],
            return_rate=a["return_rate"],
            recent=a["recent_clients"],
        )
    ]
    if a["top_hours"]:
        lines.append(tr("stats_hours", hours=", ".join(f"{hr:02d}:00 ({c})" for hr, c in a["top_hours"])))
    if a["top_weekdays"]:
        lines.append(tr("stats_days", days=", ".join(f"{WEEKDAYS[tr.lang][d]} ({c})" for d, c in a["top_weekdays"])))
    if a["per_barber"]:
        lines += ["", tr("stats_barbers")]
        lines += [
            f"💈 {h(p['name'])}: {p['count']} · {money(p['revenue'], tr)}" for p in a["per_barber"]
        ]
    await message.answer("\n".join(lines))


# ======================= clients =======================


@router.message(F.text.in_(variants("btn_clients")))
async def clients_menu(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    total = await session.scalar(
        select(func.count()).select_from(User).where(User.shop_id == shop.id, User.phone.is_not(None))
    )
    blocked = await session.scalar(
        select(func.count()).select_from(User).where(User.shop_id == shop.id, User.blocked.is_(True))
    )
    await state.set_state(OwnerStates.client_search)
    b = InlineKeyboardBuilder()
    if blocked:
        b.button(text=tr("btn_blocked_list", n=blocked), callback_data=OwCB(act="blocked"))
    b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
    b.adjust(1)
    await message.answer(tr("clients_menu", total=total or 0, blocked=blocked or 0), reply_markup=b.as_markup())


def _clients_kb(users) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in users:
        b.button(text=f"{u.name or '—'} · {u.phone or '—'}"[:60], callback_data=CardCB(act="view", client=u.id))
    b.adjust(1)
    return b.as_markup()


@router.message(OwnerStates.client_search, F.text, ~F.text.in_(MENU_TEXTS))
async def clients_search(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    q = message.text.strip()[:50]
    digits = "".join(ch for ch in q if ch.isdigit())
    conds = [User.name.ilike(f"%{q}%")]
    if len(digits) >= 3:
        conds.append(User.phone.like(f"%{digits}%"))
    users = (
        await session.scalars(
            select(User)
            .where(User.shop_id == shop.id, User.phone.is_not(None) | User.tg_id.is_(None), or_(*conds))
            .order_by(User.id.desc())
            .limit(10)
        )
    ).all()
    if not users:
        await message.answer(tr("clients_not_found"))
        return
    await state.clear()
    await message.answer(tr("clients_found", n=len(users)), reply_markup=_clients_kb(users))


@router.callback_query(OwCB.filter(F.act == "blocked"))
async def clients_blocked(cb: CallbackQuery, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    await state.clear()
    users = (
        await session.scalars(select(User).where(User.shop_id == shop.id, User.blocked.is_(True)).limit(30))
    ).all()
    await cb.message.answer(tr("blocked_title"), reply_markup=_clients_kb(users))
    await cb.answer()


# ======================= fill today's free slots =======================


async def _today_free_lines(session: AsyncSession, shop: Shop) -> list[str]:
    services = await active_services(session, shop.id)
    if not services:
        return []
    shortest = min(s.duration for s in services)
    lines = []
    for br in await bookable_barbers(session, shop.id):
        slots = await free_slots(session, br.id, today(), shortest, shop.slot_step)
        if slots:
            shown = ", ".join(f"{t:%H:%M}" for t in slots[:8]) + ("…" if len(slots) > 8 else "")
            lines.append(f"💈 {h(br.name)}: {shown}")
    return lines


async def _fill_recipients(session: AsyncSession, shop: Shop) -> list[User]:
    current = now()
    staff = {shop.owner_tg_id} | set(
        (await session.scalars(select(Barber.tg_id).where(Barber.shop_id == shop.id, Barber.tg_id.is_not(None)))).all()
    )
    recent = set(
        (
            await session.scalars(
                select(Appointment.client_id).where(
                    Appointment.shop_id == shop.id,
                    Appointment.status == "done",
                    Appointment.client_id.is_not(None),
                    Appointment.start_at >= current - timedelta(days=90),
                )
            )
        ).all()
    )
    upcoming = set(
        (
            await session.scalars(
                select(Appointment.client_id).where(
                    Appointment.shop_id == shop.id,
                    Appointment.status.in_(ACTIVE),
                    Appointment.start_at > current,
                )
            )
        ).all()
    )
    ids = recent - upcoming
    if not ids:
        return []
    users = (
        await session.scalars(
            select(User).where(User.id.in_(ids), User.blocked.is_(False), User.tg_id.is_not(None))
        )
    ).all()
    return [u for u in users if u.tg_id not in staff]


@router.message(F.text.in_(variants("btn_fill")))
async def fill_preview(message: Message, session: AsyncSession, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    if shop.last_broadcast == today():
        await message.answer(tr("fill_already"))
        return
    lines = await _today_free_lines(session, shop)
    if not lines:
        await message.answer(tr("fill_none"))
        return
    recipients = await _fill_recipients(session, shop)
    if not recipients:
        await message.answer(tr("fill_no_recipients", slots="\n".join(lines)))
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr("btn_send_n", n=len(recipients)), callback_data=OwCB(act="fill_send").pack())],
            [InlineKeyboardButton(text=tr("btn_cancel"), callback_data=FSM_CANCEL)],
        ]
    )
    await message.answer(tr("fill_preview", slots="\n".join(lines), n=len(recipients)), reply_markup=kb)


@router.callback_query(OwCB.filter(F.act == "fill_send"))
async def fill_send(cb: CallbackQuery, session: AsyncSession, shop: Shop, tr: Tr, bot: Bot):
    if shop.last_broadcast == today():
        await cb.answer(tr("fill_already"), show_alert=True)
        return
    lines = await _today_free_lines(session, shop)
    recipients = await _fill_recipients(session, shop)
    if not lines or not recipients:
        await cb.answer(tr("fill_none"), show_alert=True)
        return
    shop.last_broadcast = today()
    await session.commit()
    await safe_edit(cb.message, tr("fill_sending", n=len(recipients)))
    await cb.answer()
    slots = "\n".join(lines)
    sent = 0
    for u in recipients:
        utr = Tr(u.lang)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=utr("btn_book"), callback_data=BookCB(act="start").pack())]]
        )
        if await safe_send(bot, u.tg_id, utr("fill_msg", shop=h(shop.name), slots=slots), reply_markup=kb):
            sent += 1
        await asyncio.sleep(0.05)  # stay far below Telegram's 30 msg/s limit
    await cb.message.answer(tr("fill_sent", sent=sent, total=len(recipients)))


# ======================= settings =======================

SETTING_FIELDS = ("name", "address", "phone", "hours_text", "location", "slot_step", "rebook_days", "max_no_shows")


def settings_text(shop: Shop, tr: Tr) -> str:
    return tr(
        "settings",
        name=h(shop.name),
        address=h(shop.address or "—"),
        phone=h(shop.phone or "—"),
        hours=h(shop.hours_text or "—"),
        location="✅" if shop.latitude is not None else "—",
        step=shop.slot_step,
        rebook=shop.rebook_days,
        max_ns=shop.max_no_shows,
        paid_until=shop.paid_until.strftime("%d.%m.%Y"),
        link=registry.client_link(shop.id),
    )


def settings_kb(tr: Tr) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for field in SETTING_FIELDS:
        b.button(text=tr("set_btn_" + field), callback_data=OwCB(act="set", val=field))
    b.adjust(2)
    return b.as_markup()


@router.message(F.text.in_(variants("btn_settings")))
async def settings(message: Message, shop: Shop, tr: Tr, state: FSMContext):
    await state.clear()
    await message.answer(settings_text(shop, tr), reply_markup=settings_kb(tr), disable_web_page_preview=True)


@router.callback_query(OwCB.filter(F.act == "set"))
async def setting_start(cb: CallbackQuery, callback_data: OwCB, state: FSMContext, tr: Tr):
    field = callback_data.val
    if field not in SETTING_FIELDS:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(OwnerStates.setting)
    await state.update_data(field=field)
    if field == "slot_step":
        b = InlineKeyboardBuilder()
        for m in (10, 15, 20, 30, 60):
            b.button(text=tr("minutes_short", m=m), callback_data=OwCB(act="set_step", val=str(m)))
        b.button(text=tr("btn_cancel"), callback_data=FSM_CANCEL)
        b.adjust(5, 1)
        kb = b.as_markup()
    else:
        kb = cancel_kb(tr)
    await cb.message.answer(tr("ask_set_" + field), reply_markup=kb)
    await cb.answer()


async def _settings_saved(target: Message, shop: Shop, tr: Tr):
    await target.answer(tr("saved") + "\n\n" + settings_text(shop, tr), reply_markup=settings_kb(tr), disable_web_page_preview=True)


@router.callback_query(OwnerStates.setting, OwCB.filter(F.act == "set_step"))
async def setting_step(cb: CallbackQuery, callback_data: OwCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    shop.slot_step = int(callback_data.val)
    await state.clear()
    await session.commit()
    await _settings_saved(cb.message, shop, tr)
    await cb.answer()


@router.message(OwnerStates.setting, F.location)
async def setting_location(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    data = await state.get_data()
    if data.get("field") != "location":
        await message.answer(tr("unexpected_input"))
        return
    shop.latitude = message.location.latitude
    shop.longitude = message.location.longitude
    await state.clear()
    await session.commit()
    await _settings_saved(message, shop, tr)


@router.message(OwnerStates.setting, F.text, ~F.text.in_(MENU_TEXTS))
async def setting_text(message: Message, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    field = (await state.get_data()).get("field")
    text = message.text.strip()
    if field == "name":
        if not 2 <= len(text) <= 120:
            await message.answer(tr("name_invalid"))
            return
        shop.name = text
    elif field == "address":
        shop.address = text[:255]
    elif field == "phone":
        shop.phone = normalize_phone(text) or text[:32]
    elif field == "hours_text":
        shop.hours_text = text[:255]
    elif field in ("rebook_days", "max_no_shows", "slot_step"):
        v = parse_int(text)
        limits = {"rebook_days": (7, 90), "max_no_shows": (0, 10), "slot_step": (5, 120)}[field]
        if v is None or not limits[0] <= v <= limits[1]:
            await message.answer(tr("range_invalid", a=limits[0], b=limits[1]))
            return
        setattr(shop, field, v)
    elif field == "location":
        await message.answer(tr("ask_set_location"))
        return
    else:
        await state.clear()
        return
    await state.clear()
    await session.commit()
    await _settings_saved(message, shop, tr)
