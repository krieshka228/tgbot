"""
handlers/orders.py — просмотр и управление заказами клиента.
"""

import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from bot.config import ADMIN_USER_ID, ADMIN_CHAT_ID
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
    lines = [f"📋 **Ваши заказы** (стр. {page + 1}/{total_pages})\n"]

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

        # ✅ НОВАЯ ЛОГИКА: кнопка отмены для всех, кроме paid и confirmed
        if order.status not in (OrderStatus.paid, OrderStatus.confirmed):
            if order.status == OrderStatus.pending:
                # Для pending: отмена + оплата
                row.append(InlineKeyboardButton(
                    f"❌ Отменить #{order.id}", callback_data=f"order:cancel:{order.id}"
                ))
                if qr_token:
                    row.append(InlineKeyboardButton(
                        f"💳 Оплатить #{order.id}", callback_data=f"payment:receipt:{order.id}"
                    ))
            elif order.status == OrderStatus.cancelled:
                # Для уже отменённых – показываем статус, но не даём отменить
                pass
            else:
                # Для draft, exported, exported – кнопка отмены
                row.append(InlineKeyboardButton(
                    f"❌ Отменить #{order.id}", callback_data=f"order:cancel:{order.id}"
                ))

        # Для confirmed – кнопка дополнить данные
        elif order.status == OrderStatus.confirmed:
            if not order.contact_phone or not order.delivery_address:
                row.append(InlineKeyboardButton(
                    f"📝 Дополнить данные #{order.id}", callback_data=f"orders:complete:{order.id}"
                ))

        if row:
            kb_buttons.append(row)

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"orders:page:{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"orders:page:{page + 1}"))
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


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена заказа пользователем (для всех статусов, кроме paid и confirmed)."""
    query = update.callback_query
    await query.answer()

    order_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await query.edit_message_text("❌ Заказ не найден.", reply_markup=kb_back_to_menu())
            return

        # ❌ НЕЛЬЗЯ ОТМЕНИТЬ ТОЛЬКО ОПЛАЧЕННЫЕ И ПОДТВЕРЖДЁННЫЕ
        if order.status in (OrderStatus.paid, OrderStatus.confirmed):
            await query.edit_message_text(
                f"❌ Нельзя отменить заказ в статусе «{order.status.value}».",
                reply_markup=kb_back_to_menu()
            )
            return

        # Если заказ уже отменён
        if order.status == OrderStatus.cancelled:
            await query.edit_message_text(
                f"ℹ️ Заказ #{order_id} уже был отменён.",
                reply_markup=kb_back_to_menu()
            )
            return

        # Возвращаем остатки товаров (если они были списаны)
        for item in order.items:
            if item.product and item.product.stock is not None:
                item.product.stock += item.quantity
                item.product.is_active = item.product.stock > 0
                item.product.in_stock = item.product.stock > 0

        order.status = OrderStatus.cancelled
        await session.commit()
        invalidate_catalog_cache()

        # Уведомляем администратора
        fio = order.full_name or (order.user.full_name if order.user else None)
        admin_text = f"❌ Клиент отменил заказ #{order_id} на {order.total_amount:.0f} ₽."
        if fio:
            admin_text += f"\nФИО: {fio}"
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text)
        except Exception as e:
            logger.warning(f"Не удалось уведомить администратора об отмене: {e}")

    # Отправляем подтверждение
    is_admin = (user_id == ADMIN_USER_ID)
    await query.edit_message_text(
        f"✅ Заказ #{order_id} успешно отменён.\n\nТовары возвращены на склад.",
        reply_markup=kb_main_menu(is_admin=is_admin),
        parse_mode=ParseMode.MARKDOWN
    )

    logger.info("order cancelled by client",
                extra={"event": "order_cancelled", "user_id": user_id, "order_id": order_id})
def register(app):
    app.add_handler(CallbackQueryHandler(orders_list, pattern='^orders:list$'))
    app.add_handler(CallbackQueryHandler(orders_page_handler, pattern='^orders:page:'))
    app.add_handler(CallbackQueryHandler(orders_complete_data, pattern='^orders:complete:'))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern='^order:cancel:'))