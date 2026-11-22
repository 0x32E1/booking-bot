"""Free-time calculation. Pure function `compute_slots` + DB wrappers."""

from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Appointment, Block, WorkHours
from ..timeutil import now, today

ACTIVE = ("booked", "confirmed")  # future visits that still need action
OCCUPYING = ("booked", "confirmed", "done")  # statuses that take the chair
MIN_LEAD_MIN = 10  # can't book a slot starting in less than 10 minutes
BOOK_DAYS_AHEAD = 14


def hm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


async def get_hours(session: AsyncSession, barber_id: int) -> dict[int, WorkHours]:
    rows = (await session.scalars(select(WorkHours).where(WorkHours.barber_id == barber_id))).all()
    return {r.weekday: r for r in rows}


async def busy_intervals(
    session: AsyncSession, barber_id: int, start: datetime, end: datetime
) -> list[tuple[datetime, datetime]]:
    appts = await session.execute(
        select(Appointment.start_at, Appointment.end_at).where(
            Appointment.barber_id == barber_id,
            Appointment.status.in_(OCCUPYING),
            Appointment.start_at < end,
            Appointment.end_at > start,
        )
    )
    blocks = await session.execute(
        select(Block.start_at, Block.end_at).where(
            Block.barber_id == barber_id, Block.start_at < end, Block.end_at > start
        )
    )
    return [(s, e) for s, e in appts] + [(s, e) for s, e in blocks]


def compute_slots(
    day: date,
    wh: WorkHours | None,
    duration: int,
    step: int,
    busy: list[tuple[datetime, datetime]],
    current: datetime,
) -> list[datetime]:
    if wh is None:
        return []
    opens = datetime.combine(day, hm(wh.opens))
    closes = datetime.combine(day, hm(wh.closes))
    dur = timedelta(minutes=duration)
    earliest = current + timedelta(minutes=MIN_LEAD_MIN)
    result: list[datetime] = []
    t = opens
    while t + dur <= closes:
        if t >= earliest and all(not (t < e and t + dur > s) for s, e in busy):
            result.append(t)
        t += timedelta(minutes=max(step, 5))
    return result


async def free_slots(session: AsyncSession, barber_id: int, day: date, duration: int, step: int) -> list[datetime]:
    wh = (await get_hours(session, barber_id)).get(day.weekday())
    if wh is None:
        return []
    start = datetime.combine(day, time.min)
    busy = await busy_intervals(session, barber_id, start, start + timedelta(days=1))
    return compute_slots(day, wh, duration, step, busy, now())


async def available_days(
    session: AsyncSession, barber_id: int, duration: int, step: int, days: int = BOOK_DAYS_AHEAD
) -> list[date]:
    hours = await get_hours(session, barber_id)
    if not hours:
        return []
    d0 = today()
    busy = await busy_intervals(
        session,
        barber_id,
        datetime.combine(d0, time.min),
        datetime.combine(d0 + timedelta(days=days), time.min),
    )
    current = now()
    result = []
    for i in range(days):
        d = d0 + timedelta(days=i)
        wh = hours.get(d.weekday())
        if wh and compute_slots(d, wh, duration, step, busy, current):
            result.append(d)
    return result
