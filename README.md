# Barbershop Bot

A Telegram bot for barbershops, built to sell as a monthly subscription.
**One process runs bots for every shop.** Each shop gets its own bot (its own name and avatar), all from one server.

- Languages: Uzbek (Latin) + Russian; each person picks their own
- Stack: Python 3.10+, aiogram 3.20, SQLAlchemy 2 + SQLite (PostgreSQL optional)
- RAM: **~120 MB** for the whole process (measured), almost the same for 1 shop or 30
- No AI, no Redis, no extra services

---

## What each person gets

### Client
- **Booking:** pick a barber → service → day → time → confirm (14 days ahead, only real free slots)
- **Reminders** 24 h and 2 h before the visit, with **✅ I'll come / ❌ Cancel** buttons
- **Rebooking nudge:** the bot learns each client's personal cycle (for example every 25 days) and offers "Book with Sardor?" when it's time
- **My bookings** with cancel, plus **Address & contacts** with a map pin
- A link for Instagram or a channel (`t.me/yourbot?start=book`) opens booking straight away

### Barber
- **📋 Today / 📆 Tomorrow:** schedule with ◀️ ▶️ day navigation. Past visits nobody marked show ⚠️
- **Appointment card:** client phone, visit count, no-shows, last haircut note
- **✅ Done:** confirm or change the price, then enter the tip, then add a note or photo to the client card
- **🚫 No-show:** after N no-shows (2 by default) the client can't book online and is asked to call
- **👤 Client card:** visits, total spent, usual cycle, notes ("#2 sides, scissors top"), photos of past cuts
- **⚡ Walk-in:** records a client who came in without booking, so earnings stay correct
- **➕ Book a client by phone:** if the client later opens the bot with the same number, their history merges automatically
- **⏰ Running late:** one tap tells all of today's remaining clients ("~15 min late")
- **⛔ Block time:** breaks or days off; clients can't book those slots
- **💰 Earnings:** today, week or month: revenue, the barber's % share, tips, total
- **🕘 Shift:** check in and check out

### Owner
- **📊 Today:** bookings, done, upcoming, no-shows, revenue, who is on shift, each barber's next client
- **💵 Salary report:** per barber: clients, revenue, % share + tips = payout, shift hours; plus the shop's share
- **💈 Barbers:** add (invite link), working hours per weekday, commission %, unlink Telegram, deactivate
- **🧾 Services:** name, price, duration
- **👥 Clients:** search by name or phone, open a card, unblock
- **📈 Stats (30 days):** average check, no-show rate, return rate, busiest hours and days, per-barber totals
- **📣 Fill free slots:** sends today's free times to recent clients who have no booking (max once a day)
- **⚙️ Settings:** name, address, phone, hours text, map location, slot step, rebook days, no-show limit
- **Automatic:** a 21:00 daily summary; barbers get a reminder about visits they didn't mark

### You (the SaaS owner)
- `add-shop` gives a 14-day trial; `extend` adds paid days
- 3 days and 1 day before expiry the owner is warned. After expiry the bot pauses (clients see "call the shop") until you extend

---

## Setup (5 minutes)

**1. Create the shop's bot.** In @BotFather: `/newbot` → copy the token. Set its name, avatar and description to the shop's branding.

**2. Get the owner's Telegram ID.** The owner sends anything to @userinfobot and gives you the number.

**3. Install**
```bash
git clone <your repo> barbershop-bot && cd barbershop-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set ADMIN_CONTACT to your Telegram
```

**4. Add the shop and run**
```bash
python -m app.cli add-shop --name "Sardor Barbershop" --token 123456:ABC... --owner-id 111222333
python -m app.main
```

**5. The owner sets up the shop in Telegram:** opens the bot → /start →
- 🧾 Services → ➕ (for example "Soch olish", 30 min, 80 000)
- 💈 Barbers → ➕ name + % → **sends the invite link to each barber**. Default hours are Mon–Sat 09:00–20:00; change them under 🕘
- ⚙️ Settings → address, phone, location
- Puts the client link from Settings into Instagram bio, a Telegram channel and the shop's QR code

Then they're done: clients book, barbers work from their phones.

---

## Admin commands

```bash
python -m app.cli list                               # all shops, paid until, EXPIRED flag
python -m app.cli extend --shop 1 --days 30          # after payment (no restart needed)
python -m app.cli disable --shop 1 / enable --shop 1
python -m app.cli set-owner --shop 1 --owner-id 444555666
python -m app.cli set-token --shop 1 --token 123:NEW # then restart
```
After `add-shop` or `set-token`, **restart** the bot so it starts the new bot: `sudo systemctl restart barberbot`.

---

## Deploy on a VPS (Ubuntu)

A **1 GB VPS** is comfortable. 512 MB also works (the bot uses ~120 MB).

```bash
sudo adduser --system --group barber
sudo mkdir -p /opt/barbershop-bot && sudo chown barber:barber /opt/barbershop-bot
# copy the project to /opt/barbershop-bot, then:
cd /opt/barbershop-bot
sudo -u barber python3 -m venv .venv
sudo -u barber .venv/bin/pip install -r requirements.txt
sudo -u barber cp .env.example .env
sudo -u barber .venv/bin/python -m app.cli add-shop --name "..." --token "..." --owner-id ...

sudo cp deploy/barberbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now barberbot
journalctl -u barberbot -f          # logs
```

**Backups:** the whole database is one file, `data/bot.db`. Copy it daily, for example with cron and `sqlite3 data/bot.db ".backup /backups/bot-$(date +%F).db"`.

**Docker (optional):** `docker build -t barberbot . && docker run -d --restart=always --env-file .env -v $PWD/data:/app/data barberbot`

---

## Project structure

```
app/
  main.py          start all shop bots + scheduler
  cli.py           admin commands (add-shop, extend, ...)
  models.py        Shop, User, Barber, WorkHours, Service, Appointment, Block, ClientNote, Shift
  texts.py         every message in uz + ru
  keyboards.py     menus and callback data
  middlewares.py   per update: DB session, shop, user, roles, subscription check
  scheduler.py     reminders, nudges, daily report, expiry notices (every 60 s)
  services/        slots (free-time engine), reports, client merge
  handlers/        common (start/registration), client, barber, cards, owner, fallback
tests/
  check_texts.py   every text key exists in both languages with all placeholders
  test_flows.py    55 end-to-end checks with a fake Telegram API (no network)
```

Run tests: `python tests/check_texts.py && python tests/test_flows.py`

---

## Good to know

- **Half-finished forms** (for example typing a price) are kept in memory. A restart clears them, and the user just taps the button again. Bookings and everything else are in the database.
- **SQLite** is fine for dozens of shops. For a lot more, set `DATABASE_URL` to PostgreSQL and `pip install asyncpg`. The code is the same.
- **Time zone** is Asia/Tashkent (change `TIMEZONE` in `.env`).
- Removing a service only hides it; past visits keep their history in reports.
