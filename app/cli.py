"""Admin commands for you (the SaaS owner).

  python -m app.cli add-shop --name "Sardor Barbershop" --token 123:ABC --owner-id 111222333
  python -m app.cli list
  python -m app.cli extend --shop 1 --days 30
  python -m app.cli disable --shop 1
  python -m app.cli enable --shop 1
  python -m app.cli set-owner --shop 1 --owner-id 444555666
  python -m app.cli set-token --shop 1 --token 123:NEW

After add-shop / set-token, restart the bot process. Extend/disable work without restart.
"""

import argparse
import asyncio
from datetime import timedelta

from sqlalchemy import select

from .db import SessionLocal, init_db
from .models import Shop
from .timeutil import today


async def _get(session, shop_id: int) -> Shop:
    shop = await session.get(Shop, shop_id)
    if shop is None:
        raise SystemExit(f"Shop {shop_id} not found")
    return shop


async def run(args: argparse.Namespace) -> None:
    await init_db()
    async with SessionLocal() as s:
        if args.cmd == "add-shop":
            exists = await s.scalar(select(Shop).where(Shop.bot_token == args.token))
            if exists:
                raise SystemExit(f"This token is already used by shop {exists.id}")
            shop = Shop(
                name=args.name,
                bot_token=args.token,
                owner_tg_id=args.owner_id,
                paid_until=today() + timedelta(days=args.trial_days),
            )
            s.add(shop)
            await s.commit()
            print(f"Shop {shop.id} created. Trial until {shop.paid_until}. Restart the bot to start it.")
        elif args.cmd == "list":
            shops = (await s.scalars(select(Shop).order_by(Shop.id))).all()
            if not shops:
                print("No shops")
            for sh in shops:
                state = "active" if sh.active else "disabled"
                expired = " EXPIRED" if sh.paid_until < today() else ""
                print(f"#{sh.id:<3} {sh.name:<30} owner={sh.owner_tg_id:<12} paid_until={sh.paid_until} {state}{expired}")
        elif args.cmd == "extend":
            shop = await _get(s, args.shop)
            shop.paid_until = max(shop.paid_until, today()) + timedelta(days=args.days)
            shop.last_expiry_notice = None
            await s.commit()
            print(f"Shop {shop.id} paid until {shop.paid_until}")
        elif args.cmd in ("disable", "enable"):
            shop = await _get(s, args.shop)
            shop.active = args.cmd == "enable"
            await s.commit()
            print(f"Shop {shop.id} {'enabled' if shop.active else 'disabled'}")
        elif args.cmd == "set-owner":
            shop = await _get(s, args.shop)
            shop.owner_tg_id = args.owner_id
            await s.commit()
            print(f"Shop {shop.id} owner is now {args.owner_id}")
        elif args.cmd == "set-token":
            shop = await _get(s, args.shop)
            shop.bot_token = args.token
            await s.commit()
            print(f"Shop {shop.id} token updated. Restart the bot.")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add-shop", help="register a new barbershop")
    a.add_argument("--name", required=True)
    a.add_argument("--token", required=True, help="bot token from @BotFather")
    a.add_argument("--owner-id", type=int, required=True, help="owner's Telegram user id (ask @userinfobot)")
    a.add_argument("--trial-days", type=int, default=14)

    sub.add_parser("list", help="list shops")

    e = sub.add_parser("extend", help="add paid days")
    e.add_argument("--shop", type=int, required=True)
    e.add_argument("--days", type=int, default=30)

    for name in ("disable", "enable"):
        x = sub.add_parser(name)
        x.add_argument("--shop", type=int, required=True)

    o = sub.add_parser("set-owner")
    o.add_argument("--shop", type=int, required=True)
    o.add_argument("--owner-id", type=int, required=True)

    t = sub.add_parser("set-token")
    t.add_argument("--shop", type=int, required=True)
    t.add_argument("--token", required=True)

    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
