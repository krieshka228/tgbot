import logging
from telegram.ext import Application
from bot.config import BOT_TOKEN, CHANNEL_ID
from bot.db import init_db
from bot.handlers import start, cart, catalog, checkout, fsm_inputs, orders, admin, posts
from datetime import time
import pytz
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def post_init(app: Application):
    """Инициализация базы данных и проверка доступа к каналу."""
    await init_db()

    # Проверка: состоит ли бот в канале
    try:
        chat = await app.bot.get_chat(CHANNEL_ID)
        logger.info(f"✅ Бот в канале: {chat.title} (ID: {chat.id})")
    except Exception as e:
        logger.error(f"❌ Бот НЕ в канале или ID неверен: {e}")


def main():
    """Создаёт и запускает бота."""
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()


    # Регистрируем все обработчики
    start.register(app)
    catalog.register(app)
    cart.register(app)
    checkout.register(app)
    fsm_inputs.register(app)
    orders.register(app)
    admin.register(app)
    posts.register(app)   # автоматическая синхронизация постов канала

    # Напоминания об оплате каждый день в 09:00 МСК
    async def daily_reminder(context):
        from reminders import send_reminders
        await send_reminders(context.bot)

    app.job_queue.run_daily(
        daily_reminder,
        time=time(hour=6, minute=0, tzinfo=pytz.timezone('Europe/Moscow'))
    )

    logger.info("Бот запущен")
    # Запускаем поллинг с явным указанием нужных типов обновлений
    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "callback_query"
        ]
    )


if __name__ == "__main__":
    main()