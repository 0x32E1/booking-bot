"""Send messages to other people in *their* language.

Keyword values may be callables `f(tr) -> str` so dates/money get formatted per recipient.
`markup` may also be a callable `f(tr) -> InlineKeyboardMarkup`.
"""

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Shop, User
from .texts import Tr
from .utils import safe_send


async def lang_for(session: AsyncSession, shop_id: int, tg_id: int) -> str:
    lang = await session.scalar(select(User.lang).where(User.shop_id == shop_id, User.tg_id == tg_id))
    return lang or "uz"


async def notify_tg(bot: Bot, session: AsyncSession, shop: Shop, tg_id: int | None, key: str, markup=None, **kw) -> bool:
    if not tg_id:
        return False
    tr = Tr(await lang_for(session, shop.id, tg_id))
    rm = markup(tr) if callable(markup) else markup
    return await safe_send(bot, tg_id, tr(key, **kw), reply_markup=rm)


async def notify_user(bot: Bot, user: User | None, key: str, markup=None, **kw) -> bool:
    if user is None or not user.tg_id:
        return False
    tr = Tr(user.lang)
    rm = markup(tr) if callable(markup) else markup
    return await safe_send(bot, user.tg_id, tr(key, **kw), reply_markup=rm)
