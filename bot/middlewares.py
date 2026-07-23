"""
middlewares.py — «промежуточный слой» для всех апдейтов.

python-telegram-bot не имеет отдельного API middleware, но тот же эффект
достигается обработчиком :class:`telegram.ext.TypeHandler`, зарегистрированным
в группе с отрицательным номером — он гарантированно выполняется ПЕРЕД всеми
прикладными обработчиками. Чтобы прервать дальнейшую обработку апдейта (например,
при флуде), middleware бросает :class:`telegram.ext.ApplicationHandlerStop`.

Здесь реализованы:
    * structured-логирование каждого входящего апдейта;
    * антифлуд / защита от брутфорса (rate limit по ``user_id``);
    * утилиты проверки прав доступа (декоратор :func:`admin_only`).
"""

from __future__ import annotations

import functools
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Deque

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ContextTypes,
    TypeHandler,
)

from bot.config import ADMIN_USER_ID, settings

logger = logging.getLogger(__name__)

# Группы middleware: чем меньше число, тем раньше выполняется обработчик.
# В рамках ОДНОЙ группы PTB выполняет только первый подходящий обработчик,
# поэтому логирование и антифлуд разнесены по разным (соседним) группам.
_GROUP_LOGGING = -100
_GROUP_ANTIFLOOD = -99

# Per-process состояние антифлуда: user_id -> времена последних апдейтов.
_hits: dict[int, Deque[float]] = defaultdict(deque)
# Когда пользователю в последний раз показали предупреждение о флуде —
# чтобы не спамить ответами на каждое заблокированное сообщение.
_last_warned: dict[int, float] = {}


def _update_kind(update: Update) -> str:
    """Краткий человекочитаемый тип апдейта (для логов)."""
    if update.callback_query:
        return "callback_query"
    if update.channel_post or update.edited_channel_post:
        return "channel_post"
    if update.message:
        return "message"
    if update.edited_message:
        return "edited_message"
    return "other"


async def logging_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Логирует каждый апдейт со структурированными полями.

    На уровне INFO пишутся только метаданные (кто/что), без полного текста —
    чтобы не утекали персональные данные. Полное содержимое доступно на DEBUG.
    """
    user = update.effective_user
    chat = update.effective_chat
    kind = _update_kind(update)

    extra = {
        "event": "update",
        "kind": kind,
        "user_id": user.id if user else None,
        "chat_id": chat.id if chat else None,
    }
    if update.callback_query:
        extra["callback"] = update.callback_query.data
    logger.info("update received", extra=extra)

    if logger.isEnabledFor(logging.DEBUG):
        msg = update.effective_message
        if msg and (msg.text or msg.caption):
            logger.debug(
                "update payload",
                extra={"user_id": user.id if user else None,
                       "text": (msg.text or msg.caption)[:200]},
            )


async def antiflood_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ограничивает частоту апдейтов от одного пользователя (sliding window).

    Защищает БД и Telegram API от перегрузки одним пользователем, а также
    служит базовой защитой от брутфорса (перебора callback'ов/команд).
    При превышении лимита апдейт отбрасывается через ``ApplicationHandlerStop``.
    Системные апдейты канала (без ``effective_user``) не лимитируются.
    """
    user = update.effective_user
    if user is None:
        return
    # Администратора не лимитируем — иначе массовая синхронизация/отчёты
    # могут упереться в лимит.
    if user.id == ADMIN_USER_ID:
        return

    now = time.monotonic()
    window = settings.rate_limit_window
    limit = settings.rate_limit_messages

    hits = _hits[user.id]
    # Выбрасываем все отметки старше окна.
    while hits and (now - hits[0]) > window:
        hits.popleft()

    if len(hits) >= limit:
        # Предупреждаем не чаще одного раза в окно.
        last = _last_warned.get(user.id, 0.0)
        if (now - last) > window:
            _last_warned[user.id] = now
            logger.warning(
                "rate limit exceeded",
                extra={"event": "rate_limit", "user_id": user.id},
            )
            try:
                if update.callback_query:
                    await update.callback_query.answer(
                        "⏳ Слишком много запросов. Подождите немного.",
                        show_alert=False,
                    )
                elif update.effective_message:
                    await update.effective_message.reply_text(
                        "⏳ Слишком много запросов. Подождите немного."
                    )
            except Exception:  # noqa: BLE001 — уведомление не критично
                pass
        raise ApplicationHandlerStop

    hits.append(now)


def admin_only(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]:
    """Декоратор: пропускает только администратора (по ``ADMIN_USER_ID``).

    Не-админам отвечает «нет доступа» и логирует попытку. Дополняет
    (а не заменяет) проверки прямо в хендлерах — defense in depth.
    """

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if not user or user.id != ADMIN_USER_ID:
            logger.warning(
                "unauthorized admin access attempt",
                extra={"event": "access_denied",
                       "user_id": user.id if user else None},
            )
            if update.callback_query:
                await update.callback_query.answer("Нет доступа.", show_alert=True)
            elif update.effective_message:
                await update.effective_message.reply_text("⛔ Нет доступа.")
            return
        return await handler(update, context)

    return wrapper


def register(app: Application) -> None:
    """Регистрирует middleware-обработчики в ранних группах."""
    app.add_handler(TypeHandler(Update, logging_middleware), group=_GROUP_LOGGING)
    app.add_handler(TypeHandler(Update, antiflood_middleware), group=_GROUP_ANTIFLOOD)
