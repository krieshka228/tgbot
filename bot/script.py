"""
clean_db.py — очистка тестовых данных из базы (без удаления товаров).
Запускать при выключенных ботах.
"""

import asyncio
from sqlalchemy import text
from bot.db import engine, init_db

async def clean():
    # Инициализируем схему (на случай, если таблиц ещё нет)
    await init_db()

    async with engine.begin() as conn:
        print("Удаляю тестовые заказы и позиции...")
        await conn.execute(text("DELETE FROM order_items"))
        await conn.execute(text("DELETE FROM orders"))

        print("Удаляю комментарии...")
        await conn.execute(text("DELETE FROM comments"))

        print("Удаляю ожидающие заказы...")
        await conn.execute(text("DELETE FROM pending_orders"))

        print("Удаляю историю использования промокодов...")
        await conn.execute(text("DELETE FROM promo_usages"))

        print("Удаляю промокоды...")
        await conn.execute(text("DELETE FROM promo_codes"))

        print("Обнуляю бонусные балансы...")
        await conn.execute(text(
            "UPDATE users SET bonus_balance = 0, bonus_balance_tg = 0, bonus_balance_max = 0"
        ))

        # Если нужно удалить всех пользователей, кроме админа (замените ID на свой):
        # print("Удаляю пользователей (кроме админа)...")
        # await conn.execute(text("DELETE FROM users WHERE id != 504486622"))

        print("Готово. База очищена, товары и настройки сохранены.")

if __name__ == "__main__":
    asyncio.run(clean())