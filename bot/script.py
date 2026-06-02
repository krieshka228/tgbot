import asyncio
from telegram import Bot

BOT_TOKEN = "8589835456:AAG2L4-DY8r2GLGARfT99cm1RWnkjAwb4AQ"
SUSPECTED_ID = -1003990354141  # попробуем с минусом

async def check():
    bot = Bot(BOT_TOKEN)
    try:
        chat = await bot.get_chat(SUSPECTED_ID)
        print(f"Название: {chat.title}")
        print(f"ID: {chat.id}")
        print(f"Тип: {chat.type}")
    except Exception as e:
        print(f"Ошибка: {e}")

asyncio.run(check())