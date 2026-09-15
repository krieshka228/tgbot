"""
main.py — точка входа Telegram-бота.
Настройка логирования, инициализация БД, регистрация обработчиков, запуск поллинга.
Поддержка прокси (TG_PROXY) для обхода блокировок.
"""

import logging
import os
from datetime import time

import pytz
from telegram.ext import Application, ContextTypes
from telegram.request import HTTPXRequest

# Настройки и БД
from bot.config import settings, CHANNEL_ID
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

logger = logging.getLogger(__name__)


async def post_init(app: Application) -> None:
    """Инициализация БД при старте."""
    await init_db()
    logger.info("database initialized", extra={"event": "startup"})

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
    """Graceful shutdown: закрываем пул БД."""
    await dispose_engine()
    logger.info("database engine disposed", extra={"event": "shutdown"})


async def _daily_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_reminders(context.bot)


def build_application() -> Application:
    """Собирает и конфигурирует приложение (без запуска)."""

    # --- Настройка прокси (если задан TG_PROXY) ---
    proxy_url = os.getenv("TG_PROXY")
    request = None
    if proxy_url:
        try:
            # Пытаемся создать request с прокси и таймаутами
            request = HTTPXRequest(
                proxy=proxy_url,
                connect_timeout=60.0,
                read_timeout=60.0,
                write_timeout=60.0,
                pool_timeout=60.0,
            )
        except TypeError:
            # Если старый синтаксис, пробуем без дополнительных параметров
            try:
                request = HTTPXRequest(proxy=proxy_url)
            except TypeError:
                # Если и так не работает, используем proxy_url (старая версия)
                request = HTTPXRequest(proxy_url=proxy_url)
        logger.info("Using proxy for Telegram API", extra={"proxy": proxy_url})
    else:
        logger.info("No proxy configured – direct connection to Telegram API")

    app = (
        Application.builder()
        .token(settings.bot_token)
        .request(request)                     # ← передаём request (если None, то без прокси)
        .concurrent_updates(256)
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

    # 4) Google Sheets: периодическая синхронизация (если настроена).
    from bot.gsheets import register_jobs
    register_jobs(app)

    # 5) Ежедневное напоминание (в 06:00 МСК).
    app.job_queue.run_daily(
        _daily_reminder,
        time=time(hour=6, minute=0, tzinfo=pytz.timezone("Europe/Moscow")),
    )

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
