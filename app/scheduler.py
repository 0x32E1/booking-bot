"""Background loop (every 60 s): visit reminders, rebooking nudges,
owner's daily report, unmarked-visit reminder for barbers, subscription notices."""

import asyncio
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import registry
from .config import ADMIN_CONTACT
from .db import SessionLocal
from .keyboards import BookCB, CliCB, reminder_kb
from .models import Appointment, Barber, Shop, User
from .notify import notify_tg
from .services.slots import ACTIVE
from .texts import Tr
from .timeutil import now
from .utils import fmt_dt, h, money, safe_send

log = logging.getLogger(__name__)

NUDGE_HOURS = (10, 20)  # only nudge between 10:00 and 20:00
NUDGE_BATCH = 50  # max nudges per shop per hour
DAILY_REPORT_HOUR = 21

_last_nudge_run: dict[int, tuple[date, int]] = {}


async def run_scheduler() -> None:
    while True:
        try:
            await tick()
        except Exception:  # noqa: BLE001
            log.exception("scheduler tick failed")
        await asyncio.sleep(60)


async def tick(current: datetime | None = None) -> None:
    current = current or now()
    async with SessionLocal() as session:
        shops = (await session.scalars(select(Shop).where(Shop.active.is_(True)))).all()
        for shop in shops:
            bot = registry.bots.get(shop.id)
            if bot is None:
                continue
            try:
                await expiry_notice(session, shop, bot, current)
                if shop.paid_until < current.date():
                    await session.commit()
                    continue
                await send_reminders(session, shop, bot, current)
                key = (current.date(), current.hour)
                if _last_nudge_run.get(shop.id) != key and NUDGE_HOURS[0] <= current.hour < NUDGE_HOURS[1]:
                    _last_nudge_run[shop.id] = key
                    await send_nudges(session, shop, bot, current)
                if current.hour >= DAILY_REPORT_HOUR and shop.last_daily_report != current.date():
                    await daily_report(session, shop, bot, current)
                await session.commit()
            except Exception:  # noqa: BLE001
                log.exception("scheduler failed for shop %s", shop.id)
                await session.rollback()


async def send_reminders(session: AsyncSession, shop: Shop, bot: Bot, current: datetime) -> int:
    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.shop_id == shop.id,
                Appointment.status.in_(ACTIVE),
                Appointment.client_id.is_not(None),
                Appointment.start_at > current,
                Appointment.start_at <= current + timedelta(hours=24),
                (Appointment.reminded_24.is_(False)) | (Appointment.reminded_2.is_(False)),
            )
        )
    ).all()
    sent = 0
    for a in appts:
        left = a.start_at - current
        if left <= timedelta(hours=2) and not a.reminded_2:
            key = "reminder_2"
            a.reminded_2 = True
            a.reminded_24 = True
        elif left > timedelta(hours=2) and not a.reminded_24:
            key = "reminder_24"
            a.reminded_24 = True
        else:
            continue
        if not a.client or not a.client.tg_id:
            continue
        tr = Tr(a.client.lang)
        text = tr(
            key,
            shop=h(shop.name),
            barber=h(a.barber.name),
            service=h(a.service.name),
            when=fmt_dt(a.start_at, tr),
            address=h(shop.address or "—"),
        )
        if await safe_send(bot, a.client.tg_id, text, reply_markup=reminder_kb(tr, a.id, a.status == "confirmed")):
            sent += 1
    return sent


def _interval_days(visits: list[datetime], default: int) -> int:
    """Personal haircut cycle: average gap of the last visits, else shop default."""
    gaps = [(visits[i] - visits[i - 1]).days for i in range(1, len(visits))][-5:]
    gaps = [g for g in gaps if g >= 3]
    if not gaps:
        return default
    return max(7, min(90, round(sum(gaps) / len(gaps))))


async def send_nudges(session: AsyncSession, shop: Shop, bot: Bot, current: datetime) -> int:
    rows = (
        await session.execute(
            select(Appointment.client_id, Appointment.start_at, Appointment.barber_id)
            .where(
                Appointment.shop_id == shop.id,
                Appointment.status == "done",
                Appointment.client_id.is_not(None),
                Appointment.start_at >= current - timedelta(days=365),
            )
            .order_by(Appointment.start_at)
        )
    ).all()
    visits: dict[int, list[tuple[datetime, int]]] = defaultdict(list)
    for cid, start, barber_id in rows:
        visits[cid].append((start, barber_id))
    if not visits:
        return 0
    upcoming = set(
        (
            await session.scalars(
                select(Appointment.client_id).where(
                    Appointment.shop_id == shop.id, Appointment.status.in_(ACTIVE), Appointment.start_at > current
                )
            )
        ).all()
    )
    staff = {shop.owner_tg_id} | set(
        (await session.scalars(select(Barber.tg_id).where(Barber.shop_id == shop.id, Barber.tg_id.is_not(None)))).all()
    )

    sent = 0
    for cid, items in visits.items():
        if cid in upcoming:
            continue
        last_dt, last_barber_id = items[-1]
        interval = _interval_days([s for s, _ in items], shop.rebook_days)
        days_since = (current - last_dt).days
        if days_since < interval or days_since > interval + 60:
            continue
        user = await session.get(User, cid)
        if user is None or user.blocked or not user.tg_id or user.tg_id in staff:
            continue
        if user.last_nudge_at is not None and user.last_nudge_at >= last_dt:
            continue
        barber = await session.get(Barber, last_barber_id)
        tr = Tr(user.lang)
        if barber is not None and barber.active:
            book_button = InlineKeyboardButton(
                text=tr("btn_book_with", barber=barber.name), callback_data=BookCB(act="b", barber=barber.id).pack()
            )
            barber_name = h(barber.name)
        else:
            book_button = InlineKeyboardButton(text=tr("btn_book"), callback_data=BookCB(act="start").pack())
            barber_name = "—"
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [book_button],
                [InlineKeyboardButton(text=tr("btn_later"), callback_data=CliCB(act="later").pack())],
            ]
        )
        await safe_send(
            bot,
            user.tg_id,
            tr("nudge", name=h(user.name or ""), days=days_since, shop=h(shop.name), barber=barber_name),
            reply_markup=kb,
        )
        user.last_nudge_at = current
        sent += 1
        if sent >= NUDGE_BATCH:
            break
        await asyncio.sleep(0.05)
    return sent


async def daily_report(session: AsyncSession, shop: Shop, bot: Bot, current: datetime) -> None:
    shop.last_daily_report = current.date()
    start = datetime.combine(current.date(), time.min)
    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.shop_id == shop.id,
                Appointment.start_at >= start,
                Appointment.start_at < start + timedelta(days=1),
            )
        )
    ).all()
    if not appts:
        return
    done = [a for a in appts if a.status == "done"]
    revenue = sum(a.price or 0 for a in done)
    tips = sum(a.tip or 0 for a in done)
    await notify_tg(
        bot,
        session,
        shop,
        shop.owner_tg_id,
        "daily_report",
        done=len(done),
        revenue=lambda t: money(revenue, t),
        tips=lambda t: money(tips, t),
        noshow=sum(1 for a in appts if a.status == "no_show"),
        cancelled=sum(1 for a in appts if a.status == "cancelled"),
    )
    unmarked: dict[int, int] = defaultdict(int)
    for a in appts:
        if a.status in ACTIVE and a.end_at < current:
            unmarked[a.barber_id] += 1
    for barber_id, n in unmarked.items():
        barber = await session.get(Barber, barber_id)
        if barber and barber.tg_id:
            await notify_tg(bot, session, shop, barber.tg_id, "unmarked_reminder", n=n)


async def expiry_notice(session: AsyncSession, shop: Shop, bot: Bot, current: datetime) -> None:
    if current.hour < 10 or shop.last_expiry_notice == current.date():
        return
    left = (shop.paid_until - current.date()).days
    if left in (3, 1, 0):
        key = "sub_expiring"
    elif left == -1:
        key = "sub_expired_notice"
    else:
        return
    shop.last_expiry_notice = current.date()
    await notify_tg(
        bot,
        session,
        shop,
        shop.owner_tg_id,
        key,
        days=left,
        date=shop.paid_until.strftime("%d.%m.%Y"),
        contact=ADMIN_CONTACT or "—",
    )
