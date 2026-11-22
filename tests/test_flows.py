"""End-to-end test with a fake Telegram API. No network needed.
Run:  python tests/test_flows.py
"""

import asyncio
import os
import pathlib
import resource
import sys
import tempfile
from datetime import datetime, time, timedelta

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP}/test.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from aiogram import Bot  # noqa: E402
from aiogram.client.default import DefaultBotProperties  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Chat, Message  # noqa: E402
from aiogram.types import User as TgUser  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import registry, scheduler  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import build_dispatcher  # noqa: E402
from app.models import Appointment, Barber, ClientNote, Service, Shop, User  # noqa: E402
from app.timeutil import now, today  # noqa: E402

OWNER, BARBER, CLIENT, CLIENT2, CLIENT3 = 1000, 2000, 3000, 4000, 5000


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.mid = 100

    async def close(self):
        pass

    async def stream_content(self, *a, **k):  # pragma: no cover
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        name = type(method).__name__
        if name == "GetMe":
            return TgUser(id=bot.id, is_bot=True, first_name="Bot", username="test_bot")
        if name in ("SendMessage", "SendLocation", "SendPhoto"):
            self.mid += 1
            return Message(
                message_id=self.mid,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        if name == "SendMediaGroup":
            self.mid += 1
            return [Message(message_id=self.mid, date=datetime.now(), chat=Chat(id=method.chat_id, type="private"))]
        return True


session = FakeSession()
bot = Bot("123456:TEST-token-abcdefghijklmnopqrstuv", session=session, default=DefaultBotProperties(parse_mode="HTML"))
dp = build_dispatcher()
uid = 0
passed = 0


def ok(cond, label):
    global passed
    if not cond:
        raise AssertionError(label)
    passed += 1
    print(f"  ✓ {label}")


def _from(user_id):
    return {"id": user_id, "is_bot": False, "first_name": f"U{user_id}"}


async def send(user_id, text=None, contact=None, photo=None, location=None):
    global uid
    uid += 1
    msg = {"message_id": uid, "date": int(datetime.now().timestamp()), "chat": {"id": user_id, "type": "private"}, "from": _from(user_id)}
    if text is not None:
        msg["text"] = text
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if contact:
        msg["contact"] = contact
    if photo:
        msg["photo"] = [{"file_id": photo, "file_unique_id": "u" + photo, "width": 100, "height": 100}]
    if location:
        msg["location"] = location
    start = len(session.calls)
    await dp.feed_raw_update(bot, {"update_id": uid, "message": msg})
    return session.calls[start:]


async def click(user_id, data):
    global uid
    uid += 1
    cq = {
        "id": str(uid),
        "from": _from(user_id),
        "chat_instance": "ci",
        "data": data,
        "message": {"message_id": 50, "date": int(datetime.now().timestamp()), "chat": {"id": user_id, "type": "private"}, "from": {"id": bot.id, "is_bot": True, "first_name": "Bot"}, "text": "x"},
    }
    start = len(session.calls)
    await dp.feed_raw_update(bot, {"update_id": uid, "callback_query": cq})
    return session.calls[start:]


def texts(calls):
    return [getattr(c, "text", "") or "" for c in calls if type(c).__name__ in ("SendMessage", "EditMessageText")]


def alerts(calls):
    return [c.text or "" for c in calls if type(c).__name__ == "AnswerCallbackQuery"]


def buttons(calls):
    out = []
    for c in calls:
        rm = getattr(c, "reply_markup", None)
        if rm is not None and hasattr(rm, "inline_keyboard"):
            for row in rm.inline_keyboard:
                for b in row:
                    out.append((b.text, b.callback_data))
    return out


def find_btn(calls, prefix):
    for text, data in buttons(calls):
        if data and data.startswith(prefix):
            return data
    raise AssertionError(f"no button {prefix}; have {[d for _, d in buttons(calls)]}")


def to(calls, chat_id):
    return [c for c in calls if getattr(c, "chat_id", None) == chat_id]


async def db_one(model, *where):
    async with SessionLocal() as s:
        return await s.scalar(select(model).where(*where))


async def db_all(model, *where):
    async with SessionLocal() as s:
        return (await s.scalars(select(model).where(*where))).all()


async def main():
    await init_db()
    async with SessionLocal() as s:
        shop = Shop(name="Test <Barber>", bot_token="123456:TEST", owner_tg_id=OWNER, paid_until=today() + timedelta(days=14), phone="+998901112233", address="Chilonzor")
        s.add(shop)
        await s.commit()
        shop_id = shop.id
    registry.register(shop_id, bot, "test_bot")

    print("Owner setup")
    c = await send(OWNER, "/start")
    ok(any("Tilni" in t for t in texts(c)), "language prompt")
    c = await click(OWNER, "lang:uz")
    ok(any("boshqaruv paneli" in t for t in texts(c)), "owner sees staff panel")
    ok("&lt;Barber&gt;" in " ".join(texts(c)), "shop name is HTML-escaped")

    await send(OWNER, "🧾 Xizmatlar")
    await click(OWNER, "ow:svc_add:0:")
    await send(OWNER, "Soch olish")
    await click(OWNER, "ow:svc_dur:0:30")
    c = await send(OWNER, "80 000")
    svc = await db_one(Service, Service.name == "Soch olish")
    ok(svc and svc.price == 80000 and svc.duration == 30, "service created (80 000 so'm, 30 min)")
    await click(OWNER, "ow:svc_add:0:")
    await send(OWNER, "Soqol")
    await send(OWNER, "20")
    await send(OWNER, "40000")

    await send(OWNER, "💈 Ustalar")
    await click(OWNER, "ow:barber_add:0:")
    await send(OWNER, "Sardor")
    c = await send(OWNER, "50")
    barber = await db_one(Barber, Barber.name == "Sardor")
    ok(barber and barber.commission_pct == 50, "barber created with 50%")
    ok(any(f"start=b_{barber.invite_code}" in t for t in texts(c)), "invite link shown")

    # working hours: every day 09:00-21:00 so tests work any weekday
    for wd in range(7):
        await click(OWNER, f"ow:hours_day:{barber.id}:{wd}")
        await send(OWNER, "09:00-21:00")
    c = await click(OWNER, f"ow:hours:{barber.id}:")
    ok(sum("09:00–21:00" in t for t, _ in buttons(c)) == 7, "hours set for all 7 days")

    print("Barber joins")
    c = await send(BARBER, f"/start b_{barber.invite_code}")
    ok(any("usta sifatida" in t for t in texts(c)), "barber linked via invite")
    ok(any("ulandi" in t for t in texts(to(c, OWNER))), "owner notified barber joined")
    await click(BARBER, "lang:uz")

    print("Client registers and books (Russian)")
    await send(CLIENT, "/start book")
    await click(CLIENT, "lang:ru")
    c = await send(CLIENT, contact={"phone_number": "998935554433", "first_name": "Ali", "user_id": CLIENT})
    ok(any("Как вас зовут" in t for t in texts(c)), "asks name after phone")
    c = await send(CLIENT, "Ali")
    ok(any("Добро пожаловать" in t for t in texts(c)), "welcome in Russian")
    ok(any("Выберите мастера" in t for t in texts(c)), "deep link ?start=book opens booking")
    u = await db_one(User, User.tg_id == CLIENT)
    ok(u.phone == "+998935554433", "phone normalized")

    c = await click(CLIENT, f"bk:b:{barber.id}:0::")
    c = await click(CLIENT, find_btn(c, f"bk:s:{barber.id}:{svc.id}"))
    day_btn = find_btn(c, "bk:d:")
    c = await click(CLIENT, day_btn)
    slot_btn = find_btn(c, "bk:t:")
    c = await click(CLIENT, slot_btn)
    ok(any("Проверьте" in t for t in texts(c)), "confirmation screen")
    c = await click(CLIENT, find_btn(c, "bk:ok:"))
    ok(any("Вы записаны" in t for t in texts(c)), "booking confirmed")
    ok(any("Yangi navbat" in t for t in texts(to(c, BARBER))), "barber notified in Uzbek")
    appt = await db_one(Appointment, Appointment.client_id == u.id)
    ok(appt and appt.status == "booked", "appointment saved")

    c = await click(CLIENT, slot_btn.replace("bk:t:", "bk:ok:"))
    ok(any("занял" in a or "уже есть" in a for a in alerts(c)), "same slot can't be booked twice")

    print("Barber day view and done flow")
    c = await click(BARBER, f"br:day:0:{appt.start_at:%Y%m%d}:")
    ok(any("Ali" in t for t in texts(c)), "barber sees client in schedule")
    c = await click(BARBER, f"br:view:{appt.id}::")
    ok(any("Oldingi tashriflar" in t for t in texts(c)), "appointment card")
    await click(BARBER, f"br:done:{appt.id}::")
    await click(BARBER, "br:price_ok:0::90000")
    c = await send(BARBER, "10 000")
    appt = await db_one(Appointment, Appointment.id == appt.id)
    ok(appt.status == "done" and appt.price == 90000 and appt.tip == 10000, "done: price 90 000 + tip 10 000")
    await click(BARBER, f"cd:note:{u.id}")
    await send(BARBER, "Yon #2, tepasi qaychida")
    await click(BARBER, f"cd:photo:{u.id}")
    await send(BARBER, photo="PHOTO1")
    notes = await db_all(ClientNote, ClientNote.client_id == u.id)
    ok(len(notes) == 2, "note + photo saved to client card")
    c = await click(BARBER, f"cd:view:{u.id}")
    ok(any("Yon #2" in t and "Tashriflar: 1" in t for t in texts(c)), "card shows note and visit count")

    print("Walk-in")
    await click(BARBER, "br:walkin:0::")
    await click(BARBER, f"br:walkin_s:{svc.id}::")
    await send(BARBER, "70000")
    c = await click(BARBER, "br:tip0:0::")
    walkins = await db_all(Appointment, Appointment.client_id.is_(None))
    ok(len(walkins) == 1 and walkins[0].price == 70000 and walkins[0].status == "done", "walk-in recorded")

    print("Menu button escapes a half-finished form")
    await click(BARBER, f"br:done:{appt.id}::")  # not active -> alert, no state
    await click(BARBER, "br:walkin:0::")
    await click(BARBER, f"br:walkin_s:{svc.id}::")
    c = await send(BARBER, "📋 Bugun")
    ok(any("Ish vaqti" in t for t in texts(c)), "menu button works mid-form")

    print("Earnings")
    c = await send(BARBER, "💰 Daromadim")
    t = " ".join(texts(c))
    ok("160 000" in t and "80 000" in t and "10 000" in t and "90 000" in t, "earnings: revenue 160k, share 80k, tips 10k, total 90k")

    print("No-shows block the client")
    async with SessionLocal() as s:
        past = now() - timedelta(hours=3)
        ids = []
        for i in range(2):
            a = Appointment(shop_id=shop_id, barber_id=barber.id, client_id=u.id, service_id=svc.id, start_at=past + timedelta(minutes=40 * i), end_at=past + timedelta(minutes=40 * i + 30), price=80000)
            s.add(a)
            await s.flush()
            ids.append(a.id)
        await s.commit()
    await click(BARBER, f"br:noshow:{ids[0]}::")
    c = await click(BARBER, f"br:noshow:{ids[1]}::")
    ok(any("yopildi" in a for a in alerts(c)), "barber told client is now blocked")
    ok(any("временно закрыта" in t for t in texts(to(c, CLIENT))), "client told in Russian")
    c = await send(CLIENT, "✂️ Записаться")
    ok(any("закрыта" in t for t in texts(c)), "blocked client can't book online")
    await click(OWNER, f"cd:unblock:{u.id}")
    u = await db_one(User, User.id == u.id)
    ok(not u.blocked and u.no_show_count == 0, "owner unblocked client")

    print("Client cancels")
    c = await send(CLIENT, "✂️ Записаться")
    c = await click(CLIENT, f"bk:b:{barber.id}:0::")
    c = await click(CLIENT, find_btn(c, f"bk:s:{barber.id}:{svc.id}"))
    days = [d for _, d in buttons(c) if d.startswith("bk:d:")]
    c = await click(CLIENT, days[-1])
    c = await click(CLIENT, find_btn(c, "bk:t:"))
    await click(CLIENT, find_btn(c, "bk:ok:"))
    c = await send(CLIENT, "📅 Мои записи")
    c = await click(CLIENT, find_btn(c, "cl:ask:"))
    c = await click(CLIENT, find_btn(c, "cl:yes:"))
    ok(any("отменена" in t for t in texts(c)), "client cancelled")
    ok(any("bekor qildi" in t for t in texts(to(c, BARBER))), "barber notified of cancellation")

    print("Manual booking by phone + merge when client joins")
    target_day = (today() + timedelta(days=2)).strftime("%Y%m%d")
    c = await click(BARBER, f"br:add:0:{target_day}:")
    c = await click(BARBER, f"br:add_s:{svc.id}:{target_day}:")
    await click(BARBER, find_btn(c, "br:add_t:"))
    await send(BARBER, "90 777 66 55")
    c = await send(BARBER, "Bobur")
    offline = await db_one(User, User.phone == "+998907776655")
    ok(offline and offline.tg_id is None, "offline client created")
    ok(any("Bobur yozildi" in t for t in texts(c)), "barber sees confirmation")
    await send(CLIENT2, "/start")
    await click(CLIENT2, "lang:uz")
    c = await send(CLIENT2, "+998 90 777 66 55")
    ok(any("Xush kelibsiz, Bobur" in t for t in texts(c)), "joined client merged (name kept, no name question)")
    merged = await db_all(User, User.phone == "+998907776655")
    ok(len(merged) == 1 and merged[0].tg_id == CLIENT2, "duplicate removed, history moved")

    print("Block time")
    block_day = (today() + timedelta(days=3)).strftime("%Y%m%d")
    c = await click(BARBER, f"br:blk_make:0:{block_day}:full")
    ok(any("Yopildi" in t for t in texts(c)), "whole day blocked")
    c = await click(CLIENT, f"bk:d:{barber.id}:{svc.id}:{block_day}:")
    ok(any("свободного времени нет" in t for t in texts(c)), "blocked day has no slots for clients")

    print("Reminders")
    async with SessionLocal() as s:
        start = datetime.combine(today() + timedelta(days=5), time(15, 0))
        a = Appointment(shop_id=shop_id, barber_id=barber.id, client_id=u.id, service_id=svc.id, start_at=start, end_at=start + timedelta(minutes=30), price=80000)
        s.add(a)
        await s.commit()
        rid = a.id
    before = len(session.calls)
    async with SessionLocal() as s:
        sh = await s.get(Shop, shop_id)
        n24 = await scheduler.send_reminders(s, sh, bot, start - timedelta(hours=23))
        n2 = await scheduler.send_reminders(s, sh, bot, start - timedelta(minutes=110))
        again = await scheduler.send_reminders(s, sh, bot, start - timedelta(minutes=100))
        await s.commit()
    ok(n24 == 1 and n2 == 1 and again == 0, "24h + 2h reminders sent exactly once")
    rem = texts(to(session.calls[before:], CLIENT))
    ok(any("завтра" in t for t in rem), "reminder in client's language")
    c = await click(CLIENT, f"cl:confirm:{rid}")
    ok((await db_one(Appointment, Appointment.id == rid)).status == "confirmed", "client confirmed from reminder")

    print("Rebooking nudge")
    async with SessionLocal() as s:
        c3 = User(shop_id=shop_id, tg_id=CLIENT3, name="Jasur", phone="+998901234567", lang="uz")
        s.add(c3)
        await s.flush()
        for days_ago in (50, 25):
            st = now() - timedelta(days=days_ago)
            s.add(Appointment(shop_id=shop_id, barber_id=barber.id, client_id=c3.id, service_id=svc.id, start_at=st, end_at=st + timedelta(minutes=30), status="done", price=80000))
        await s.commit()
    before = len(session.calls)
    async with SessionLocal() as s:
        sh = await s.get(Shop, shop_id)
        sent = await scheduler.send_nudges(s, sh, bot, now())
        sent2 = await scheduler.send_nudges(s, sh, bot, now())
        await s.commit()
    nudges = texts(to(session.calls[before:], CLIENT3))
    ok(sent == 1 and sent2 == 0 and any("25 kun" in t for t in nudges), "nudge after personal 25-day cycle, only once")
    ok(any(d and d.startswith(f"bk:b:{barber.id}") for _, d in buttons(to(session.calls[before:], CLIENT3))), "nudge has 'book with Sardor' button")

    print("Owner reports")
    c = await send(OWNER, "📊 Bugungi holat")
    ok(any("Sardor" in t for t in texts(c)), "dashboard")
    c = await send(OWNER, "💵 Maosh hisoboti")
    ok(any("To'lanadi" in t for t in texts(c)), "salary report")
    c = await click(OWNER, "ow:salary:0:week")
    ok(any("Shu hafta" in t for t in texts(c)), "salary for week")
    c = await send(OWNER, "📈 Statistika")
    ok(any("Oxirgi 30 kun" in t for t in texts(c)), "analytics")
    await send(OWNER, "👥 Mijozlar")
    c = await send(OWNER, "5554")
    ok(any(d == f"cd:view:{u.id}" for _, d in buttons(c)), "client search by phone digits")
    await send(OWNER, "⚙️ Sozlamalar")
    await click(OWNER, "ow:set:0:address")
    c = await send(OWNER, "Yunusobod 4")
    ok(any("Yunusobod 4" in t for t in texts(c)), "settings saved")
    await click(OWNER, "ow:set:0:location")
    await send(OWNER, location={"latitude": 41.31, "longitude": 69.28})
    c = await send(CLIENT, "📍 Адрес и контакты")
    ok(any(type(x).__name__ == "SendLocation" for x in c), "client gets map location")

    print("Fill free slots")
    async with SessionLocal() as s:  # make CLIENT eligible: no upcoming bookings
        for a in (await s.scalars(select(Appointment).where(Appointment.client_id == u.id, Appointment.status.in_(("booked", "confirmed"))))).all():
            a.status = "cancelled"
        await s.commit()
    c = await send(OWNER, "📣 Bo'sh vaqtlarni to'ldirish")
    tx = " ".join(texts(c))
    if "Bugun bo'sh vaqt yo'q" in tx:
        print("  (late in the day: no free slots left today — skipped)")
    else:
        c = await click(OWNER, "ow:fill_send:0:")
        ok(any("Yuborildi" in t for t in texts(c)), "broadcast sent")
        c = await send(OWNER, "📣 Bo'sh vaqtlarni to'ldirish")
        ok(any("allaqachon" in t for t in texts(c)), "only once per day")

    print("Daily report + subscription")
    async with SessionLocal() as s:
        sh = await s.get(Shop, shop_id)
        before = len(session.calls)
        await scheduler.daily_report(s, sh, bot, datetime.combine(today(), time(21, 5)))
        await s.commit()
    ok(any("Kun yakuni" in t for t in texts(to(session.calls[before:], OWNER))), "owner daily report")
    async with SessionLocal() as s:
        sh = await s.get(Shop, shop_id)
        sh.paid_until = today() - timedelta(days=1)
        await s.commit()
    c = await send(CLIENT, "✂️ Записаться")
    ok(any("временно не работает" in t for t in texts(c)), "expired shop: clients see paused")
    c = await send(OWNER, "📊 Bugungi holat")
    ok(any("Obuna muddati tugagan" in t for t in texts(c)), "expired shop: owner told to renew")

    c = await click(CLIENT, "br:view:1::")  # client pressing staff button
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    print(f"\nALL {passed} CHECKS PASSED. Peak RAM of this process: {rss:.0f} MB")


asyncio.run(main())
