"""
reminders.py — ежедневные напоминания об оплате.

Каждый день в 06:00 МСК (задаётся в main.py) запускается задача,
которая находит заказы в статусе pending (ожидают оплаты), по которым
ещё не было отправлено 2 напоминания, и у которых с момента обновления
прошло более 1 дня. Отправляет пользователю уведомление с предложением
оплатить или отменить заказ.
"""

import logging

from bot.db import get_session, get_unpaid_orders_for_reminder

logger = logging.getLogger(__name__)


async def send_reminders(bot):
    """
    Отправляет напоминания об оплате всем пользователям,
    у которых есть подходящие неоплаченные заказы.
    """
    async for session in get_session():
        orders = await get_unpaid_orders_for_reminder(session)
        logger.info(f"Напоминаний к отправке: {len(orders)}")
        for order in orders:
            user = order.user
            if not user:
                continue
            count = order.reminder_sent_count + 1
            text = (
                f"⏰ **Напоминание #{count}:** у вас есть неоплаченный заказ "
                f"#{order.id} на {order.total_amount:.0f} ₽.\n\n"
                "Оплатите или отмените его. Напишите /start для открытия меню."
            )
            try:
                await bot.send_message(
                    chat_id=user.id,
                    text=text,
                    parse_mode="Markdown"
                )
                order.reminder_sent_count += 1
                await session.commit()
            except Exception as e:
                logger.warning(f"Не удалось отправить напоминание пользователю {user.id}: {e}")