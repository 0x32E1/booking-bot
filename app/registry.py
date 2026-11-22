"""In-memory map of running bots. One process serves every shop."""

from aiogram import Bot

bots: dict[int, Bot] = {}  # shop_id -> Bot
shop_by_bot: dict[int, int] = {}  # bot_id -> shop_id
usernames: dict[int, str] = {}  # shop_id -> bot username


def register(shop_id: int, bot: Bot, username: str) -> None:
    bots[shop_id] = bot
    shop_by_bot[bot.id] = shop_id
    usernames[shop_id] = username or ""


def client_link(shop_id: int) -> str:
    return f"https://t.me/{usernames.get(shop_id, '')}?start=book"


def invite_link(shop_id: int, code: str) -> str:
    return f"https://t.me/{usernames.get(shop_id, '')}?start=b_{code}"
