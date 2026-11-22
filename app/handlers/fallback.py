from aiogram import Router
from aiogram.filters import StateFilter
from aiogram.types import CallbackQuery, Message

from ..keyboards import main_menu
from ..models import User
from ..texts import Tr
from .common import is_registered

router = Router(name="fallback")


@router.message(StateFilter(None))
async def unknown(message: Message, user: User | None, roles: set[str], tr: Tr):
    if not is_registered(user, roles):
        await message.answer(tr("press_start"))
        return
    await message.answer(tr("unknown"), reply_markup=main_menu(tr, roles))


@router.message()
async def unexpected_in_state(message: Message, tr: Tr):
    await message.answer(tr("unexpected_input"))


@router.callback_query()
async def stale_button(cb: CallbackQuery, tr: Tr):
    await cb.answer(tr("outdated"))
