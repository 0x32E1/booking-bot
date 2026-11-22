"""Views shared by several handler modules."""

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .keyboards import BookCB
from .models import Barber, Service, Shop, WorkHours
from .texts import Tr


async def bookable_barbers(session: AsyncSession, shop_id: int) -> list[Barber]:
    barbers = (
        await session.scalars(
            select(Barber).where(Barber.shop_id == shop_id, Barber.active.is_(True)).order_by(Barber.id)
        )
    ).all()
    if not barbers:
        return []
    with_hours = set(
        (
            await session.scalars(
                select(WorkHours.barber_id).where(WorkHours.barber_id.in_([b.id for b in barbers]))
            )
        ).all()
    )
    return [b for b in barbers if b.id in with_hours]


async def active_services(session: AsyncSession, shop_id: int) -> list[Service]:
    return list(
        (
            await session.scalars(
                select(Service).where(Service.shop_id == shop_id, Service.active.is_(True)).order_by(Service.id)
            )
        ).all()
    )


async def barbers_view(session: AsyncSession, shop: Shop, tr: Tr) -> tuple[str, InlineKeyboardMarkup | None]:
    barbers = await bookable_barbers(session, shop.id)
    if not barbers:
        return tr("no_barbers"), None
    b = InlineKeyboardBuilder()
    for br in barbers:
        b.button(text=f"💈 {br.name}", callback_data=BookCB(act="b", barber=br.id))
    b.adjust(1)
    return tr("choose_barber"), b.as_markup()
