"""Run every shop's bot in one process:  python -m app.main"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from sqlalchemy import select

from . import registry
from .config import LOG_LEVEL
from .db import SessionLocal, init_db
from .handlers import barber, cards, client, common, fallback, owner
from .middlewares import ContextMiddleware
from .models import Shop
from .scheduler import run_scheduler

log = logging.getLogger("barberbot")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(ContextMiddleware())
    # Order matters: common first (/start, registration), fallback last.
    dp.include_routers(common.router, owner.router, barber.router, cards.router, client.router, fallback.router)
    return dp


async def main() -> None:
    logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await init_db()

    async with SessionLocal() as session:
        shops = (await session.scalars(select(Shop).where(Shop.active.is_(True)))).all()
    if not shops:
        log.error("No shops yet. Add one:  python -m app.cli add-shop --name ... --token ... --owner-id ...")
        return

    bots: list[Bot] = []
    for shop in shops:
        bot = Bot(shop.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            me = await bot.get_me()
        except Exception as e:  # noqa: BLE001
            log.error("Shop %s (%s): bad token or Telegram unreachable: %s", shop.id, shop.name, e)
            await bot.session.close()
            continue
        registry.register(shop.id, bot, me.username)
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="start", description="Boshlash / Начать"),
                    BotCommand(command="menu", description="Menyu / Меню"),
                ]
            )
        except Exception:  # noqa: BLE001
            log.warning("Could not set commands for @%s", me.username)
        bots.append(bot)
        log.info("Shop %s '%s' -> @%s", shop.id, shop.name, me.username)

    if not bots:
        log.error("No bot could start. Check tokens with: python -m app.cli list")
        return

    dp = build_dispatcher()
    scheduler = asyncio.create_task(run_scheduler())
    try:
        await dp.start_polling(*bots, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()
        for bot in bots:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
