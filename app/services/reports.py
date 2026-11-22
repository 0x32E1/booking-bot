from collections import Counter
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Appointment, Shift, User
from ..timeutil import now

PERIODS = ("today", "week", "month", "last_month")


def period_range(kind: str, d: date) -> tuple[datetime, datetime]:
    if kind == "week":
        s = d - timedelta(days=d.weekday())
        e = s + timedelta(days=7)
    elif kind == "month":
        s = d.replace(day=1)
        e = (s + timedelta(days=32)).replace(day=1)
    elif kind == "last_month":
        e = d.replace(day=1)
        s = (e - timedelta(days=1)).replace(day=1)
    else:  # today
        s = d
        e = d + timedelta(days=1)
    return datetime.combine(s, time.min), datetime.combine(e, time.min)


async def barber_stats(session: AsyncSession, barber_id: int, start: datetime, end: datetime) -> dict:
    rows = (
        await session.execute(
            select(Appointment.status, Appointment.price, Appointment.tip).where(
                Appointment.barber_id == barber_id,
                Appointment.start_at >= start,
                Appointment.start_at < end,
            )
        )
    ).all()
    done = [r for r in rows if r.status == "done"]
    return {
        "count": len(done),
        "revenue": sum(r.price or 0 for r in done),
        "tips": sum(r.tip or 0 for r in done),
        "no_shows": sum(1 for r in rows if r.status == "no_show"),
    }


async def shift_minutes(session: AsyncSession, barber_id: int, start: datetime, end: datetime) -> int:
    shifts = (
        await session.scalars(
            select(Shift).where(
                Shift.barber_id == barber_id,
                Shift.start_at < end,
                (Shift.end_at.is_(None)) | (Shift.end_at > start),
            )
        )
    ).all()
    current = now()
    total = 0
    for s in shifts:
        s_start = max(s.start_at, start)
        s_end = min(s.end_at or current, end, current)
        if s_end > s_start:
            total += int((s_end - s_start).total_seconds() // 60)
    return total


async def shop_analytics(session: AsyncSession, shop_id: int) -> dict:
    """Last 30 days of numbers for the owner."""
    current = now()
    since = current - timedelta(days=30)
    appts = (
        await session.scalars(
            select(Appointment).where(
                Appointment.shop_id == shop_id,
                Appointment.start_at >= since,
                Appointment.start_at <= current,
            )
        )
    ).all()
    done = [a for a in appts if a.status == "done"]
    no_shows = [a for a in appts if a.status == "no_show"]
    cancelled = [a for a in appts if a.status == "cancelled"]
    revenue = sum(a.price or 0 for a in done)

    # Return rate: of clients seen in the last 90 days, how many have 2+ visits ever
    since90 = current - timedelta(days=90)
    recent_ids = set(
        (
            await session.scalars(
                select(Appointment.client_id).where(
                    Appointment.shop_id == shop_id,
                    Appointment.status == "done",
                    Appointment.client_id.is_not(None),
                    Appointment.start_at >= since90,
                )
            )
        ).all()
    )
    returning = 0
    if recent_ids:
        counts = dict(
            (
                await session.execute(
                    select(Appointment.client_id, func.count())
                    .where(Appointment.client_id.in_(recent_ids), Appointment.status == "done")
                    .group_by(Appointment.client_id)
                )
            ).all()
        )
        returning = sum(1 for cid in recent_ids if counts.get(cid, 0) >= 2)

    new_clients = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(User.shop_id == shop_id, User.created_at >= since, User.phone.is_not(None))
    )

    per_barber: dict[int, dict] = {}
    for a in done:
        d = per_barber.setdefault(a.barber_id, {"name": a.barber.name, "count": 0, "revenue": 0})
        d["count"] += 1
        d["revenue"] += a.price or 0

    return {
        "done": len(done),
        "revenue": revenue,
        "avg_check": revenue // len(done) if done else 0,
        "no_show_rate": round(100 * len(no_shows) / (len(done) + len(no_shows))) if (done or no_shows) else 0,
        "cancelled": len(cancelled),
        "walkins": sum(1 for a in done if a.client_id is None),
        "top_hours": Counter(a.start_at.hour for a in done).most_common(3),
        "top_weekdays": Counter(a.start_at.weekday() for a in done).most_common(3),
        "return_rate": round(100 * returning / len(recent_ids)) if recent_ids else 0,
        "recent_clients": len(recent_ids),
        "new_clients": new_clients or 0,
        "per_barber": sorted(per_barber.values(), key=lambda x: -x["revenue"]),
    }
