import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import Update
from sqlalchemy import select

from . import registry
from .config import ADMIN_CONTACT
from .db import SessionLocal
from .models import Barber, Shop, User
from .texts import Tr
from .timeutil import today

log = logging.getLogger(__name__)


class ContextMiddleware(BaseMiddleware):
    """For every update: open a DB session, find the shop (by bot), the user and their roles."""

    async def __call__(
        self,
        handler: Callable[[Update, dict[str, Any]], Awaitable[Any]],
        event: Update,
        data: dict[str, Any],
    ) -> Any:
        bot: Bot = data["bot"]
        shop_id = registry.shop_by_bot.get(bot.id)
        if shop_id is None:
            return None
        if event.message and event.message.chat.type != "private":
            return None

        tg_user = data.get("event_from_user")
        async with SessionLocal() as session:
            shop = await session.get(Shop, shop_id)
            if shop is None:
                return None

            user = barber = None
            roles: set[str] = set()
            if tg_user is not None:
                user = await session.scalar(select(User).where(User.shop_id == shop.id, User.tg_id == tg_user.id))
                if tg_user.id == shop.owner_tg_id:
                    roles.add("owner")
                barber = await session.scalar(
                    select(Barber).where(
                        Barber.shop_id == shop.id, Barber.tg_id == tg_user.id, Barber.active.is_(True)
                    )
                )
                if barber is not None:
                    roles.add("barber")

            tr = Tr(user.lang if user and user.lang else "uz")

            if not shop.active or shop.paid_until < today():
                await self._paused(event, tr, "owner" in roles)
                return None

            data.update(session=session, shop=shop, user=user, barber=barber, roles=roles, tr=tr)
            result = await handler(event, data)
            await session.commit()
            return result

    @staticmethod
    async def _paused(event: Update, tr: Tr, is_owner: bool) -> None:
        text = tr("sub_expired_owner", contact=ADMIN_CONTACT or "—") if is_owner else tr("bot_paused")
        try:
            if event.callback_query:
                await event.callback_query.answer(text[:190], show_alert=True)
            elif event.message:
                await event.message.answer(text)
        except Exception:  # noqa: BLE001
            log.debug("could not send paused notice", exc_info=True)
