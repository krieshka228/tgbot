import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from bot.db import get_session, OrderStatus, Order, OrderItem
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from bot.keyboards import kb_back_to_menu, kb_payment

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
        # Общее количество заказов (кроме draft)
        total_stmt = select(Order).where(Order.user_id == user_id, Order.status != OrderStatus.draft)
        total = (await session.execute(select(func.count()).select_from(total_stmt.subquery()))).scalar()

        # Загружаем заказы для текущей страницы
        stmt = (
            select(Order)
            .where(Order.user_id == user_id, Order.status != OrderStatus.draft)
            .options(selectinload(Order.items).selectinload(OrderItem.product))
            .order_by(Order.created_at.desc())
            .offset(page * ORDERS_PER_PAGE)
            .limit(ORDERS_PER_PAGE)
        )
        orders = (await session.execute(stmt)).scalars().all()

    if total == 0:
        await query.edit_message_text("📋 У вас пока нет оформленных заказов.", reply_markup=kb_back_to_menu())
        return

    total_pages = (total - 1) // ORDERS_PER_PAGE + 1
    lines = [f"📋 **Ваши заказы** (стр. {page+1}/{total_pages})\n"]

    for order in orders:
        label = STATUS_LABEL.get(order.status.value, order.status.value)
        qty = sum(i.quantity for i in order.items)
        lines.append(
            f"• Заказ #{order.id} — {label}\n"
            f"  {qty} поз. на {order.total_amount:.0f} ₽"
            + (f"\n  Адрес: {order.delivery_address}" if order.delivery_address else "")
        )

    # Собираем клавиатуру: для каждого заказа свои кнопки
    kb_buttons = []
    for order in orders:
        if order.status == OrderStatus.pending:
            kb_buttons.append([
                InlineKeyboardButton(f"💳 Оплатить #{order.id}", callback_data=f"payment:receipt:{order.id}"),
                InlineKeyboardButton(f"❌ Отменить #{order.id}", callback_data=f"payment:cancel:{order.id}")
            ])
        elif order.status == OrderStatus.paid:
            kb_buttons.append([
                InlineKeyboardButton(f"❌ Отменить #{order.id}", callback_data=f"payment:cancel:{order.id}")
            ])

    # Навигационные кнопки
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


def register(app):
    app.add_handler(CallbackQueryHandler(orders_list, pattern='^orders:list$'))
    app.add_handler(CallbackQueryHandler(orders_page_handler, pattern='^orders:page:'))
    # Обработчики payment:receipt и payment:cancel уже должны быть зарегистрированы в checkout.py