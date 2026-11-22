"""/start, language, client registration, barber invite links, /menu, cancel."""

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..keyboards import FSM_CANCEL, LangCB, lang_kb, main_menu, name_kb, phone_kb
from ..models import Barber, Shop, User
from ..notify import notify_tg
from ..services.clients import merge_offline
from ..texts import LANG_PROMPT, Tr, variants
from ..utils import h, normalize_phone, safe_edit
from ..views import barbers_view

router = Router(name="common")


class Reg(StatesGroup):
    phone = State()
    name = State()


def is_registered(user: User | None, roles: set[str]) -> bool:
    return bool(roles) or bool(user and user.phone and user.name)


async def continue_onboarding(
    target: Message,
    session: AsyncSession,
    shop: Shop,
    user: User,
    roles: set[str],
    state: FSMContext,
    tg_name: str = "",
) -> None:
    """Ask for whatever is missing (phone, name), then show the main menu."""
    tr = Tr(user.lang)
    if roles:
        await state.clear()
        await target.answer(tr("staff_welcome", shop=h(shop.name)), reply_markup=main_menu(tr, roles))
        return
    if not user.phone:
        await state.set_state(Reg.phone)
        await target.answer(tr("ask_phone"), reply_markup=phone_kb(tr))
        return
    if not user.name:
        await state.set_state(Reg.name)
        await target.answer(tr("ask_name"), reply_markup=name_kb(tg_name))
        return
    data = await state.get_data()
    await state.clear()
    await target.answer(tr("welcome", name=h(user.name), shop=h(shop.name)), reply_markup=main_menu(tr, roles))
    if data.get("pending") == "book":
        if user.blocked:
            await target.answer(tr("client_blocked", phone=h(shop.phone or "—")))
            return
        text, kb = await barbers_view(session, shop, tr)
        await target.answer(text, reply_markup=kb)


async def _link_barber(
    message: Message, session: AsyncSession, shop: Shop, user: User, roles: set[str], code: str, bot: Bot
) -> None:
    tr = Tr(user.lang)
    uid = message.from_user.id
    barber = await session.scalar(select(Barber).where(Barber.shop_id == shop.id, Barber.invite_code == code))
    if barber is None or (barber.tg_id and barber.tg_id != uid):
        await message.answer(tr("invite_invalid"))
        return
    first_time = barber.tg_id != uid
    barber.tg_id = uid
    if not user.name:
        user.name = barber.name
    await session.commit()
    if barber.active:
        roles.add("barber")
    await message.answer(tr("invite_ok", name=h(barber.name), shop=h(shop.name)))
    if first_time and shop.owner_tg_id != uid:
        await notify_tg(bot, session, shop, shop.owner_tg_id, "barber_joined", name=h(barber.name))


@router.message(CommandStart())
async def cmd_start(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
    shop: Shop,
    user: User | None,
    roles: set[str],
    state: FSMContext,
    bot: Bot,
) -> None:
    await state.clear()
    args = (command.args or "").strip()
    if user is None:
        user = User(shop_id=shop.id, tg_id=message.from_user.id)
        session.add(user)
        await session.commit()
    if args.startswith("b_"):
        await _link_barber(message, session, shop, user, roles, args[2:], bot)
    await state.update_data(pending="book" if args == "book" else "")
    if not user.lang:
        await message.answer(LANG_PROMPT, reply_markup=lang_kb())
        return
    await continue_onboarding(message, session, shop, user, roles, state, message.from_user.full_name)


@router.callback_query(LangCB.filter())
async def on_lang(
    cb: CallbackQuery,
    callback_data: LangCB,
    session: AsyncSession,
    shop: Shop,
    user: User | None,
    roles: set[str],
    state: FSMContext,
) -> None:
    if user is None:
        user = User(shop_id=shop.id, tg_id=cb.from_user.id)
        session.add(user)
    user.lang = callback_data.code if callback_data.code in ("uz", "ru") else "uz"
    await session.commit()
    tr = Tr(user.lang)
    await safe_edit(cb.message, tr("lang_set"))
    await cb.answer()
    await continue_onboarding(cb.message, session, shop, user, roles, state, cb.from_user.full_name)


@router.message(F.text.in_(variants("btn_lang")))
async def lang_button(message: Message) -> None:
    await message.answer(LANG_PROMPT, reply_markup=lang_kb())


@router.message(Reg.phone, F.contact)
async def on_contact(
    message: Message, session: AsyncSession, shop: Shop, user: User | None, roles: set[str], state: FSMContext, tr: Tr
) -> None:
    if user is None:
        return
    contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer(tr("phone_not_yours"), reply_markup=phone_kb(tr))
        return
    user.phone = normalize_phone(contact.phone_number) or contact.phone_number
    await merge_offline(session, user)
    await session.commit()
    await continue_onboarding(message, session, shop, user, roles, state, message.from_user.full_name)


@router.message(Reg.phone, F.text)
async def on_phone_text(
    message: Message, session: AsyncSession, shop: Shop, user: User | None, roles: set[str], state: FSMContext, tr: Tr
) -> None:
    if user is None:
        return
    phone = normalize_phone(message.text)
    if not phone:
        await message.answer(tr("phone_invalid"), reply_markup=phone_kb(tr))
        return
    user.phone = phone
    await merge_offline(session, user)
    await session.commit()
    await continue_onboarding(message, session, shop, user, roles, state, message.from_user.full_name)


@router.message(Reg.name, F.text)
async def on_name(
    message: Message, session: AsyncSession, shop: Shop, user: User | None, roles: set[str], state: FSMContext, tr: Tr
) -> None:
    if user is None:
        return
    name = " ".join(message.text.split())
    if len(name) < 2 or len(name) > 60 or name.startswith("/"):
        await message.answer(tr("name_invalid"))
        return
    user.name = name
    await session.commit()
    await continue_onboarding(message, session, shop, user, roles, state)


@router.message(Command("menu", "cancel"))
async def cmd_menu(
    message: Message, session: AsyncSession, shop: Shop, user: User | None, roles: set[str], state: FSMContext, tr: Tr
) -> None:
    if user is None or not user.lang:
        await state.clear()
        await message.answer(LANG_PROMPT, reply_markup=lang_kb())
        return
    if not is_registered(user, roles):
        await continue_onboarding(message, session, shop, user, roles, state, message.from_user.full_name)
        return
    await state.clear()
    await message.answer(tr("menu"), reply_markup=main_menu(tr, roles))


@router.callback_query(F.data == FSM_CANCEL)
async def on_fsm_cancel(cb: CallbackQuery, state: FSMContext, tr: Tr) -> None:
    await state.clear()
    await safe_edit(cb.message, tr("action_cancelled"))
    await cb.answer()
