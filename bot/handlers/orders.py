from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler
from bot.db import get_session, OrderStatus, Order, OrderItem
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from bot.keyboards import kb_back_to_menu, kb_main_menu
from bot.filters import StateFilter

STATUS_LABEL = {
    "pending": "⏳ Ожидает оплаты",
    "paid": "💳 Оплачен (проверяется)",
    "confirmed": "✅ Подтверждён",
    "exported": "🚚 В обработке",
    "cancelled": "❌ Отменён",
}

async def orders_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    async for session in get_session():
        stmt = (
            select(Order)
            .where(Order.user_id == user_id, Order.status != OrderStatus.draft)
            .options(selectinload(Order.items).selectinload(OrderItem.product))
            .order_by(Order.created_at.desc())
            .limit(10)
        )
        orders = (await session.execute(stmt)).scalars().all()
    if not orders:
        await query.edit_message_text("📋 У вас пока нет оформленных заказов.", reply_markup=kb_back_to_menu())
        return
    lines = ["📋 **Ваши заказы:**\n"]
    for order in orders:
        label = STATUS_LABEL.get(order.status.value, order.status.value)
        qty = sum(i.quantity for i in order.items)
        lines.append(
            f"• Заказ #{order.id} — {label}\n"
            f"  {qty} поз. на {order.total_amount:.0f} ₽"
            + (f"\n  Адрес: {order.delivery_address}" if order.delivery_address else "")
        )
    await query.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back_to_menu())

async def contact_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'contact_admin'
    await query.edit_message_text("✉️ Напишите ваш вопрос — передадим администратору:")

def register(app):
    app.add_handler(CallbackQueryHandler(orders_list, pattern='^orders:list$'))
    app.add_handler(CallbackQueryHandler(contact_admin_start, pattern='^contact:admin$'))