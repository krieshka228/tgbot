"""
main.py — точка входа Telegram-бота.
Настройка логирования, инициализация БД, регистрация обработчиков, запуск поллинга.
"""

import asyncio
import logging
from datetime import time

import pytz
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

# Настройки и БД
from bot.config import settings, ADMIN_USER_ID, CHANNEL_ID
from bot.db import init_db, dispose_engine
from bot.logging_config import setup_logging

# Middleware (логирование, антифлуд)
from bot import middlewares

# Обработчики
from bot.handlers import (
    start,
    catalog,
    cart,
    checkout,
    fsm_inputs,
    orders,
    admin,
    posts,
)
from bot.error_handler import error_handler
from bot.reminders import send_reminders
from bot.utils import logger

# Для загрузки фото в Max
#from aiomax import Bot as MaxBot

logger = logging.getLogger(__name__)


async def post_init(app: Application) -> None:
    """Инициализация БД и MaxBot при старте."""
    await init_db()
    logger.info("database initialized", extra={"event": "startup"})

    # Инициализируем сессию MaxBot
    max_bot = app.bot_data.get("max_bot")
    if max_bot:
        await max_bot.start()
        logger.info("MaxBot session started")

    # Проверка доступа к каналу
    try:
        chat = await app.bot.get_chat(CHANNEL_ID)
        logger.info(
            "channel access OK",
            extra={"event": "channel_check", "channel_id": chat.id, "title": chat.title},
        )
    except Exception as exc:
        logger.error(
            "channel access FAILED",
            extra={"event": "channel_check", "error": repr(exc)},
        )

async def post_shutdown(app: Application) -> None:
    """Graceful shutdown: закрываем сессию MaxBot и пул БД."""
    max_bot = app.bot_data.get("max_bot")
    if max_bot:
        await max_bot.close()
        logger.info("MaxBot session closed")
    await dispose_engine()
    logger.info("database engine disposed", extra={"event": "shutdown"})


async def _daily_reminder(context) -> None:
    await send_reminders(context.bot)


def build_application() -> Application:
    """Собирает и конфигурирует приложение (без запуска)."""
    app = (
        Application.builder()
        .token(settings.bot_token)
        .concurrent_updates(256)
        .connection_pool_size(512)
        .pool_timeout(30.0)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # 1) Middleware (ранние группы): логирование + антифлуд.
    middlewares.register(app)

    # 2) Прикладные обработчики (группа 0).
    start.register(app)
    catalog.register(app)
    cart.register(app)
    checkout.register(app)
    fsm_inputs.register(app)
    orders.register(app)
    admin.register(app)
    posts.register(app)  # автоматическая синхронизация постов канала

    # 3) Глобальный обработчик ошибок.
    app.add_error_handler(error_handler)

    # 4) Ежедневное напоминание (в 06:00 МСК).
    app.job_queue.run_daily(
        _daily_reminder,
        time=time(hour=6, minute=0, tzinfo=pytz.timezone("Europe/Moscow")),
    )

    # 5) Создаём MaxBot для загрузки медиа и сохраняем в bot_data
    #max_bot = MaxBot(settings.max_bot_token)
    #app.bot_data["max_bot"] = max_bot
    #logger.info("MaxBot instance created for media uploads")

    return app


def main() -> None:
    """Точка входа: настраивает логирование, валидирует конфиг, запускает бота."""
    setup_logging(level=settings.log_level, json_format=settings.log_json)

    # Fail-fast: не запускаем бота с неполной/битой конфигурацией.
    settings.assert_production_ready()

    app = build_application()
    logger.info("bot starting", extra={"event": "startup"})

    app.run_polling(
        allowed_updates=[
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "callback_query",
        ]
    )


if __name__ == "__main__":
    main()