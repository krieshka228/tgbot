"""
handlers/orders.py — просмотр и управление заказами клиента.
"""

import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode

from bot.db import get_session, OrderStatus, Order, OrderItem, get_order_with_items, get_bot_setting
from bot.keyboards import kb_back_to_menu, kb_payment
from bot.utils import escape_markdown
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

logger = logging.getLogger(__name__)

ORDERS_PER_PAGE = 5

STATUS_LABEL = {
    "pending":   "⏳ Ожидает оплаты",
    "paid":      "💳 Оплачен (проверяется)",
    "confirmed": "✅ Подтверждён",
    "exported":  "🚚 В обработке",
    "cancelled": "❌ Отменён",
}


async def orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Показывает список заказов клиента с постраничной навигацией и действиями."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async for session in get_session():
        total_stmt = select(Order).where(Order.user_id == user_id, Order.status != OrderStatus.draft)
        total = (await session.execute(select(func.count()).select_from(total_stmt.subquery()))).scalar()

        stmt = (
            select(Order)
            .where(Order.user_id == user_id, Order.status != OrderStatus.draft)
            .options(selectinload(Order.items).selectinload(OrderItem.product))
            .order_by(Order.created_at.desc())
            .offset(page * ORDERS_PER_PAGE)
            .limit(ORDERS_PER_PAGE)
        )
        orders = (await session.execute(stmt)).scalars().all()
        qr_token = await get_bot_setting(session, "payment_qr_token")

    if total == 0:
        await query.edit_message_text("📋 У вас пока нет оформленных заказов.", reply_markup=kb_back_to_menu())
        return

    total_pages = (total - 1) // ORDERS_PER_PAGE + 1
    lines = [f"📋 **Ваши заказы** (стр. {page+1}/{total_pages})\n"]
    now = datetime.now(timezone.utc)
    CANCEL_TIMEOUT = 300  # 5 минут

    for order in orders:
        label = STATUS_LABEL.get(order.status.value, order.status.value)
        qty = sum(i.quantity for i in order.items)
        address_line = f"\n  Адрес: {escape_markdown(order.delivery_address)}" if order.delivery_address else ""
        lines.append(
            f"• Заказ #{order.id} — {label}\n"
            f"  {qty} поз. на {order.total_amount:.0f} ₽"
            + address_line
        )

    kb_buttons = []
    for order in orders:
        row = []
        if order.status == OrderStatus.pending:
            # Исправление: делаем created_at offset-aware
            created_at = order.created_at.replace(tzinfo=timezone.utc)
            age_seconds = (now - created_at).total_seconds()
            if age_seconds < CANCEL_TIMEOUT:
                row.append(InlineKeyboardButton(
                    f"❌ Отменить #{order.id}", callback_data=f"payment:cancel:{order.id}"))
            if qr_token:
                row.append(InlineKeyboardButton(
                    f"💳 Оплатить #{order.id}", callback_data=f"payment:receipt:{order.id}"))
        elif order.status == OrderStatus.confirmed:
            if not order.contact_phone or not order.delivery_address:
                row.append(InlineKeyboardButton(
                    f"📝 Дополнить данные #{order.id}", callback_data=f"orders:complete:{order.id}"))
        if row:
            kb_buttons.append(row)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"orders:page:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"orders:page:{page+1}"))
    if nav_buttons:
        kb_buttons.append(nav_buttons)

    kb_buttons.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    keyboard = InlineKeyboardMarkup(kb_buttons)

    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)


async def orders_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик переключения страниц."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[2])
    await orders_list(update, context, page)


async def orders_complete_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await query.edit_message_text("❌ Заказ не найден.", reply_markup=kb_back_to_menu())
            return
        missing_full_name = not order.full_name
        missing_phone = not order.contact_phone
        missing_address = not order.delivery_address

    context.user_data['data'] = {'order_id': order_id}
    if missing_full_name:
        context.user_data['state'] = 'awaiting_full_name'
        prompt = "✏️ Введите ваше полное имя (ФИО):"
    elif missing_phone:
        context.user_data['state'] = 'awaiting_phone'
        prompt = "📱 Введите номер телефона для связи:"
    elif missing_address:
        context.user_data['state'] = 'awaiting_address'
        prompt = "📍 Введите адрес доставки:"
    else:
        await query.edit_message_text("✅ Все данные заказа уже заполнены.", reply_markup=kb_back_to_menu())
        return

    logger.info("orders: complete data start",
                extra={"event": "order_complete_data", "user_id": user_id,
                       "order_id": order_id,
                       "field": "full_name" if missing_full_name else ("phone" if missing_phone else "address")})
    await query.edit_message_text(prompt, reply_markup=kb_back_to_menu())


def register(app):
    app.add_handler(CallbackQueryHandler(orders_list, pattern='^orders:list$'))
    app.add_handler(CallbackQueryHandler(orders_page_handler, pattern='^orders:page:'))
    app.add_handler(CallbackQueryHandler(orders_complete_data, pattern='^orders:complete:'))