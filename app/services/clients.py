from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Appointment, ClientNote, User


async def merge_offline(session: AsyncSession, user: User) -> None:
    """A barber may have added this person by phone before they opened the bot.
    When they share the same phone, move that history onto their real account."""
    if not user.phone:
        return
    others = (
        await session.scalars(
            select(User).where(
                User.shop_id == user.shop_id,
                User.phone == user.phone,
                User.tg_id.is_(None),
                User.id != user.id,
            )
        )
    ).all()
    for other in others:
        await session.execute(
            update(Appointment).where(Appointment.client_id == other.id).values(client_id=user.id)
        )
        await session.execute(
            update(ClientNote).where(ClientNote.client_id == other.id).values(client_id=user.id)
        )
        user.no_show_count += other.no_show_count
        user.blocked = user.blocked or other.blocked
        if not user.name:
            user.name = other.name
        await session.delete(other)


async def find_by_phone(session: AsyncSession, shop_id: int, phone: str) -> User | None:
    return await session.scalar(
        select(User).where(User.shop_id == shop_id, User.phone == phone).order_by(User.tg_id.is_(None)).limit(1)
    )
