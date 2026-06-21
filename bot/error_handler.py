"""
error_handler.py — единый обработчик необработанных исключений.

Регистрируется через ``application.add_error_handler``. Любое исключение,
вылетевшее из хендлера, попадает сюда: оно логируется с полным трейсбеком,
администратору отправляется краткое уведомление, а пользователю — вежливое
сообщение о сбое (вместо «зависшего» интерфейса).
"""

from __future__ import annotations

import html
import logging
import traceback

from telegram import Update
from telegram.error import Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from bot.config import ADMIN_CHAT_ID

logger = logging.getLogger(__name__)

# Ошибки сети/таймаутов — обыденность при поллинге; их логируем мягче и не
# дёргаем ими администратора.
_TRANSIENT = (TimedOut, NetworkError, RetryAfter)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует исключение и уведомляет пользователя/администратора."""
    error = context.error

    if isinstance(error, _TRANSIENT):
        logger.warning(
            "transient error",
            extra={"event": "transient_error", "error": repr(error)},
        )
        return

    logger.error(
        "unhandled exception",
        exc_info=error,
        extra={"event": "exception"},
    )

    # Пытаемся вежливо ответить пользователю, не «глотая» вторичные ошибки.
    if isinstance(update, Update):
        try:
            if update.callback_query:
                await update.callback_query.answer(
                    "⚠️ Произошла ошибка. Попробуйте позже.", show_alert=True
                )
            elif update.effective_message:
                await update.effective_message.reply_text(
                    "⚠️ Произошла ошибка. Мы уже разбираемся, попробуйте позже."
                )
        except Forbidden:
            pass  # пользователь заблокировал бота
        except Exception:  # noqa: BLE001
            logger.debug("failed to notify user about error", exc_info=True)

    # Краткое уведомление администратору (в HTML, чтобы не падать на разметке).
    if ADMIN_CHAT_ID:
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )[-1500:]
        text = (
            "🚨 <b>Необработанная ошибка</b>\n"
            f"<pre>{html.escape(tb)}</pre>"
        )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML"
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to notify admin about error", exc_info=True)
