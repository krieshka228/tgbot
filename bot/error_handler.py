"""
error_handler.py — единый обработчик необработанных исключений.

Регистрируется через ``application.add_error_handler``. Любое исключение,
вылетевшее из хендлера, попадает сюда: оно логируется с полным трейсбеком,
администратору отправляется краткое уведомление, а пользователю — вежливое
сообщение о сбое (вместо «зависшего» интерфейса).

Отдельно обрабатываются ошибки поллинга, которые НЕ означают поломку бота:

* :class:`~telegram.error.Conflict` — «terminated by other getUpdates
  request». Возникает, когда тот же токен опрашивает второй процесс
  (дубль контейнера, локальный запуск ``python -m bot.main``, старый процесс
  после ребута). PTB сам переподключается, поэтому без троттлинга ошибка
  превращается в поток сообщений администратору (наблюдались ~8 с между ними).
* :class:`~telegram.error.Forbidden` — пользователь заблокировал бота;
  это штатная ситуация, а не сбой.

Для повторяющихся ошибок введён троттлинг уведомлений
(:data:`_NOTIFY_COOLDOWN_SEC`): администратор получает одно сообщение в час
на каждый класс ошибки, а не по одному на каждое повторение. В лог при этом
пишется каждое событие — диагностику троттлинг не ухудшает.
"""

from __future__ import annotations

import html
import logging
import time
import traceback

from telegram import Update
from telegram.error import Conflict, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.ext import ContextTypes

from bot.config import ADMIN_CHAT_ID

logger = logging.getLogger(__name__)

# Ошибки сети/таймаутов — обыденность при поллинге; их логируем мягче и не
# дёргаем ими администратора.
_TRANSIENT = (TimedOut, NetworkError, RetryAfter)

# Пауза между повторными уведомлениями администратора об ОДНОЙ И ТОЙ ЖЕ
# повторяющейся ошибке.
_NOTIFY_COOLDOWN_SEC = 3600.0

# Счётчик подавленных уведомлений: key -> сколько раз промолчали с последней
# отправки. Показываем его в следующем уведомлении, чтобы админ видел масштаб.
_suppressed: dict[str, int] = {}

# key -> monotonic time последней отправки уведомления администратору.
_last_notified: dict[str, float] = {}


def _now() -> float:
    """Точка отсчёта для троттлинга. Выделена функцией, чтобы тесты могли её
    подменять, не патча глобальный модуль :mod:`time`."""
    return time.monotonic()


def _throttle(key: str) -> tuple[bool, int]:
    """Решает, отправлять ли уведомление по этому ключу.

    :return: ``(отправлять, сколько уведомлений подавлено с прошлой отправки)``.
    """
    now = _now()
    prev = _last_notified.get(key)
    if prev is not None and (now - prev) < _NOTIFY_COOLDOWN_SEC:
        _suppressed[key] = _suppressed.get(key, 0) + 1
        return False, _suppressed[key]
    _last_notified[key] = now
    suppressed = _suppressed.pop(key, 0)
    return True, suppressed


# Текст подсказки: Conflict всегда означает второго поллера на том же токене.
_CONFLICT_HINT = (
    "Причина: тот же токен опрашивает второй процесс. Проверьте, что запущен "
    "только один контейнер бота и нет локального запуска `python -m bot.main`.\n"
    "Бот сам переподключается; уведомления об этой ошибке приходят не чаще "
    "раза в час."
)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует исключение и уведомляет пользователя/администратора."""
    error = context.error

    if isinstance(error, _TRANSIENT):
        logger.warning(
            "transient error",
            extra={"event": "transient_error", "error": repr(error)},
        )
        return

    # Forbidden — пользователь заблокировал бота / бот исключён из чата.
    # Штатная ситуация: логируем на INFO, админа не дёргаем.
    if isinstance(error, Forbidden) and not isinstance(error, Conflict):
        logger.info(
            "bot blocked by user or chat",
            extra={"event": "forbidden", "error": repr(error)},
        )
        return

    # Conflict — второй getUpdates на том же токене. Логируем всегда (это
    # сигнал о дубле инстанса), но уведомление админу троттлируем.
    if isinstance(error, Conflict):
        send, suppressed = _throttle("conflict")
        logger.warning(
            "getUpdates conflict: another poller is using this token",
            extra={
                "event": "polling_conflict",
                "error": repr(error),
                "notified_admin": send,
                "suppressed_since_last": suppressed,
            },
        )
        if send and ADMIN_CHAT_ID:
            text = (
                "⚠️ <b>Telegram: Conflict (terminated by other getUpdates request)</b>\n"
                f"<pre>{html.escape(repr(error))}</pre>\n"
                f"{html.escape(_CONFLICT_HINT)}"
            )
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML"
                )
            except Exception:  # noqa: BLE001
                logger.debug("failed to notify admin about conflict", exc_info=True)
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
    # Одна и та же повторяющаяся ошибка — не чаще раза в час, со счётчиком
    # подавленных повторов.
    if ADMIN_CHAT_ID:
        exc_key = f"{type(error).__name__}"
        send, suppressed = _throttle(exc_key)
        if not send:
            return
        tb = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )[-1500:]
        text = (
            "🚨 <b>Необработанная ошибка</b>\n"
            f"<pre>{html.escape(tb)}</pre>"
        )
        if suppressed:
            text += (
                f"\n<i>Повторилась {suppressed} раз(а) с прошлого уведомления — "
                f"следующее придёт не раньше чем через час.</i>"
            )
        try:
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID, text=text, parse_mode="HTML"
            )
        except Exception:  # noqa: BLE001
            logger.debug("failed to notify admin about error", exc_info=True)
