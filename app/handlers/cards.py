"""Client card: history, haircut notes, photos. Used by barbers and the owner."""

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InputMediaPhoto, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..filters import RoleFilter
from ..keyboards import MENU_TEXTS, CardCB, cancel_kb
from ..models import Appointment, Barber, ClientNote, Shop, User
from ..texts import Tr
from ..utils import fmt_dt, h, money

router = Router(name="cards")
router.message.filter(RoleFilter("barber", "owner"))
router.callback_query.filter(RoleFilter("barber", "owner"))


class CardStates(StatesGroup):
    note = State()
    photo = State()


async def _client(session: AsyncSession, shop: Shop, client_id: int) -> User | None:
    u = await session.get(User, client_id)
    return u if u and u.shop_id == shop.id else None


async def render_card(session: AsyncSession, client: User, tr: Tr, roles: set[str]):
    done_filter = (Appointment.client_id == client.id, Appointment.status == "done")
    visits = (
        await session.scalars(select(Appointment).where(*done_filter).order_by(Appointment.start_at.desc()).limit(11))
    ).all()
    total = await session.scalar(select(func.count()).select_from(Appointment).where(*done_filter)) or 0
    spent = await session.scalar(select(func.coalesce(func.sum(Appointment.price), 0)).where(*done_filter)) or 0

    lines = [
        tr("card_title", name=h(client.name or "—")),
        tr("card_phone", phone=h(client.phone or "—")),
        tr("card_visits", n=total, spent=money(spent, tr)),
    ]
    if visits:
        last = visits[0]
        lines.append(
            tr("card_last", when=fmt_dt(last.start_at, tr), service=h(last.service.name), barber=h(last.barber.name))
        )
        if len(visits) >= 2:
            gaps = [(visits[i].start_at - visits[i + 1].start_at).days for i in range(len(visits) - 1)]
            lines.append(tr("card_interval", days=round(sum(gaps) / len(gaps))))
    if client.no_show_count:
        lines.append(tr("card_noshows", n=client.no_show_count))
    if client.blocked:
        lines.append(tr("card_blocked"))
    if not client.tg_id:
        lines.append(tr("card_offline"))

    notes = (
        await session.scalars(
            select(ClientNote)
            .where(ClientNote.client_id == client.id, ClientNote.text.is_not(None))
            .order_by(ClientNote.id.desc())
            .limit(5)
        )
    ).all()
    if notes:
        lines += ["", tr("card_notes")]
        lines += [f"• <i>{n.created_at:%d.%m.%y}</i> {h(n.text)}" for n in notes]

    photo_count = await session.scalar(
        select(func.count())
        .select_from(ClientNote)
        .where(ClientNote.client_id == client.id, ClientNote.photo_file_id.is_not(None))
    ) or 0

    b = InlineKeyboardBuilder()
    b.button(text=tr("btn_add_note"), callback_data=CardCB(act="note", client=client.id))
    b.button(text=tr("btn_add_photo"), callback_data=CardCB(act="photo", client=client.id))
    if photo_count:
        b.button(text=tr("btn_photos", n=photo_count), callback_data=CardCB(act="photos", client=client.id))
    if "owner" in roles and client.blocked:
        b.button(text=tr("btn_unblock"), callback_data=CardCB(act="unblock", client=client.id))
    b.adjust(2)
    return "\n".join(lines), b.as_markup()


@router.callback_query(CardCB.filter(F.act == "view"))
async def card_view(cb: CallbackQuery, callback_data: CardCB, session: AsyncSession, shop: Shop, roles: set[str], tr: Tr):
    client = await _client(session, shop, callback_data.client)
    if client is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    text, kb = await render_card(session, client, tr, roles)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(CardCB.filter(F.act == "note"))
async def note_start(cb: CallbackQuery, callback_data: CardCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    client = await _client(session, shop, callback_data.client)
    if client is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(CardStates.note)
    await state.update_data(client_id=client.id)
    await cb.message.answer(tr("ask_note", name=h(client.name or "—")), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(CardStates.note, F.text, ~F.text.in_(MENU_TEXTS))
async def note_save(message: Message, session: AsyncSession, shop: Shop, barber: Barber | None, state: FSMContext, tr: Tr):
    data = await state.get_data()
    client = await _client(session, shop, data.get("client_id") or 0)
    await state.clear()
    if client is None:
        await message.answer(tr("not_available"))
        return
    session.add(ClientNote(client_id=client.id, barber_id=barber.id if barber else None, text=message.text[:1000]))
    await session.commit()
    await message.answer(tr("note_saved"))


@router.callback_query(CardCB.filter(F.act == "photo"))
async def photo_start(cb: CallbackQuery, callback_data: CardCB, session: AsyncSession, shop: Shop, state: FSMContext, tr: Tr):
    client = await _client(session, shop, callback_data.client)
    if client is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    await state.set_state(CardStates.photo)
    await state.update_data(client_id=client.id)
    await cb.message.answer(tr("ask_photo", name=h(client.name or "—")), reply_markup=cancel_kb(tr))
    await cb.answer()


@router.message(CardStates.photo, F.photo)
async def photo_save(message: Message, session: AsyncSession, shop: Shop, barber: Barber | None, state: FSMContext, tr: Tr):
    data = await state.get_data()
    client = await _client(session, shop, data.get("client_id") or 0)
    await state.clear()
    if client is None:
        await message.answer(tr("not_available"))
        return
    session.add(
        ClientNote(
            client_id=client.id,
            barber_id=barber.id if barber else None,
            photo_file_id=message.photo[-1].file_id,
            text=(message.caption or None) and message.caption[:1000],
        )
    )
    await session.commit()
    await message.answer(tr("photo_saved"))


@router.message(CardStates.photo, ~F.text.in_(MENU_TEXTS))
async def photo_expected(message: Message, tr: Tr):
    await message.answer(tr("photo_expected"))


@router.callback_query(CardCB.filter(F.act == "photos"))
async def photos(cb: CallbackQuery, callback_data: CardCB, session: AsyncSession, shop: Shop, tr: Tr):
    client = await _client(session, shop, callback_data.client)
    if client is None:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    notes = (
        await session.scalars(
            select(ClientNote)
            .where(ClientNote.client_id == client.id, ClientNote.photo_file_id.is_not(None))
            .order_by(ClientNote.id.desc())
            .limit(10)
        )
    ).all()
    if not notes:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    media = [
        InputMediaPhoto(media=n.photo_file_id, caption=f"{n.created_at:%d.%m.%Y}" + (f" — {n.text}" if n.text else ""))
        for n in notes
    ]
    await cb.message.answer_media_group(media)
    await cb.answer()


@router.callback_query(CardCB.filter(F.act == "unblock"))
async def unblock(cb: CallbackQuery, callback_data: CardCB, session: AsyncSession, shop: Shop, roles: set[str], tr: Tr):
    client = await _client(session, shop, callback_data.client)
    if client is None or "owner" not in roles:
        await cb.answer(tr("not_available"), show_alert=True)
        return
    client.blocked = False
    client.no_show_count = 0
    await session.commit()
    text, kb = await render_card(session, client, tr, roles)
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer(tr("unblocked"))
