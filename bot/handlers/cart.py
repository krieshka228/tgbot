import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from bot.db import get_session, get_or_create_user, get_draft_order, remove_item_from_order, recalculate_total, OrderStatus
from bot.keyboards import kb_cart_actions, kb_cart_items_remove, kb_back_to_menu
from bot.utils import format_cart
from bot.config import PAYMENT_DETAILS
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from bot.db import Product, Order, OrderItem   # если используется Product (теперь да)

logger = logging.getLogger(__name__)


async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр текущей корзины."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    # Удаляем предыдущее сообщение корзины, если оно было сохранено
    prev_msg_id = context.user_data.pop('cart_message_id', None)
    if prev_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=prev_msg_id)
        except Exception:
            pass

    async for session in get_session():
        user = await get_or_create_user(session, user_id)
        if not user.consented:
            await query.edit_message_text("❌ Сначала дайте согласие на обработку данных (/start).")
            return
        order = await get_draft_order(session, user_id)

    if order is None or not order.items:
        msg = await query.edit_message_text("🛒 Ваша корзина пуста.\nПерейдите в каталог и добавьте товары.",
                                             reply_markup=kb_back_to_menu())
    else:
        msg = await query.edit_message_text(format_cart(order),
                                            parse_mode=ParseMode.MARKDOWN,
                                            reply_markup=kb_cart_actions(order.id, has_items=True))
    context.user_data['cart_message_id'] = msg.message_id


async def cart_remove_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список позиций для удаления."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_draft_order(session, user_id)
    if not order or not order.items:
        await query.edit_message_text("🛒 Корзина пуста.")
        return
    await query.edit_message_text("Выберите позицию для удаления:",
                                   reply_markup=kb_cart_items_remove(order))


async def cart_delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет выбранный товар из корзины."""
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_draft_order(session, user_id)
        if not order:
            await query.edit_message_text("🛒 Корзина пуста.")
            return
        removed = await remove_item_from_order(session, order, item_id)
        if removed:
            await session.refresh(order)
            if order.items:
                await query.edit_message_text("✅ Удалено.\n\n" + format_cart(order),
                                              parse_mode=ParseMode.MARKDOWN,
                                              reply_markup=kb_cart_actions(order.id))
            else:
                await query.edit_message_text("✅ Удалено. Корзина пуста.", reply_markup=kb_back_to_menu())
        else:
            await query.edit_message_text("❌ Позиция не найдена.")


async def cart_edit_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор товара для изменения количества."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_draft_order(session, user_id)
    if not order or not order.items:
        await query.answer("Корзина пуста.", show_alert=True)
        return
    buttons = []
    for item in order.items:
        name = item.product.name if item.product else f"Товар #{item.product_id}"
        buttons.append([InlineKeyboardButton(f"{name} (x{item.quantity})", callback_data=f"cart:change_qty:{item.id}")])
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="cart:view")])
    await query.edit_message_text("Выберите позицию для изменения:",
                                   reply_markup=InlineKeyboardMarkup(buttons))


async def cart_change_qty_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает интерфейс изменения количества (шаг +/− или ввод)."""
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_draft_order(session, user_id)
    if not order:
        await query.answer("Корзина пуста.", show_alert=True)
        return
    item = next((i for i in order.items if i.id == item_id), None)
    if not item:
        await query.answer("Позиция не найдена.", show_alert=True)
        return
    current_qty = item.quantity
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("-5", callback_data=f"cart:delta:{item_id}:-5"),
         InlineKeyboardButton("-1", callback_data=f"cart:delta:{item_id}:-1"),
         InlineKeyboardButton("+1", callback_data=f"cart:delta:{item_id}:+1"),
         InlineKeyboardButton("+5", callback_data=f"cart:delta:{item_id}:+5")],
        [InlineKeyboardButton("🔢 Ввести число", callback_data=f"cart:input:{item_id}")],
        [InlineKeyboardButton("↩️ Назад", callback_data="cart:view")]
    ])
    await query.edit_message_text(f"Количество: **{current_qty}**\nВыберите действие:",
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=kb)


async def cart_delta(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки +/− для изменения количества."""
    query = update.callback_query
    await query.answer()
    _, _, item_id, delta = query.data.split(":")
    item_id, delta = int(item_id), int(delta)
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_draft_order(session, user_id)
        if not order:
            await query.edit_message_text("🛒 Корзина пуста.")
            return
        item = next((i for i in order.items if i.id == item_id), None)
        if not item:
            await query.answer("Позиция не найдена.", show_alert=True)
            return
        product = item.product
        new_qty = item.quantity + delta
        if product and product.stock is not None and new_qty > product.stock:
            await query.answer(f"❌ Доступно только {product.stock} шт.", show_alert=True)
            return
        if new_qty <= 0:
            order.items.remove(item)
            await session.delete(item)
        else:
            item.quantity = new_qty
        await recalculate_total(session, order)
        await session.commit()
    async for session in get_session():
        order = await get_draft_order(session, user_id)
    if not order or not order.items:
        await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
        context.user_data.pop('state', None)
        return
    await query.edit_message_text(format_cart(order),
                                   parse_mode=ParseMode.MARKDOWN,
                                   reply_markup=kb_cart_actions(order.id))


async def cart_input_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переводит в режим ручного ввода количества."""
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":")[-1])
    context.user_data['state'] = 'cart_change_qty'
    context.user_data['data'] = {'item_id': item_id}
    await query.edit_message_text("✏️ Введите новое количество (целое число):", reply_markup=kb_back_to_menu())


# ---------- НОВЫЙ ЭТАП: ПОДТВЕРЖДЕНИЕ ЗАКАЗА ----------
async def cart_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает сводку заказа с кнопками «Подтвердить» и «Изменить»."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    # Удаляем предыдущее сообщение корзины
    prev_msg_id = context.user_data.pop('cart_message_id', None)
    if prev_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=prev_msg_id)
        except Exception:
            pass

    async for session in get_session():
        order = await get_draft_order(session, user_id)
        if not order or not order.items:
            await query.edit_message_text("🛒 Корзина пуста.")
            return

    # Показываем подтверждение
    cart_text = format_cart(order)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"checkout:confirm:{order.id}")],
        [InlineKeyboardButton("✏️ Изменить заказ", callback_data="cart:view")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])
    await query.message.reply_text(
        f"📋 **Проверьте ваш заказ:**\n\n{cart_text}\n\nВсё верно?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )
    # Удаляем сообщение, с которого был вызван checkout
    try:
        await query.message.delete()
    except Exception:
        pass


async def checkout_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальное подтверждение с атомарным резервированием товаров."""
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    async for session in get_session():
        # Загружаем заказ с позициями и товарами
        stmt = (
            select(Order)
            .where(Order.id == order_id, Order.user_id == user_id, Order.status == OrderStatus.draft)
            .options(selectinload(Order.items).selectinload(OrderItem.product))
        )
        result = await session.execute(stmt)
        order = result.scalar_one_or_none()

        if not order:
            await query.edit_message_text("❌ Заказ не найден или уже оформлен.")
            return

        # Получаем id всех товаров в заказе
        product_ids = [item.product_id for item in order.items]

        # Блокируем строки продуктов для атомарной проверки
        lock_stmt = select(Product).where(Product.id.in_(product_ids)).with_for_update()
        locked_products = (await session.execute(lock_stmt)).scalars().all()
        product_map = {p.id: p for p in locked_products}

        # Проверяем остатки
        for item in order.items:
            product = product_map.get(item.product_id)
            if product and product.stock is not None and item.quantity > product.stock:
                await query.edit_message_text(
                    f"❌ К сожалению, товар «{product.name}» уже разобрали. "
                    f"Доступно: {product.stock} шт. Пожалуйста, измените количество.",
                    reply_markup=kb_cart_actions(order.id)
                )
                return

        # Списываем остатки и обновляем активность товаров
        for item in order.items:
            product = product_map[item.product_id]
            if product and product.stock is not None:
                product.stock -= item.quantity
                product.is_active = product.stock > 0
                product.in_stock = product.stock > 0

        # Меняем статус заказа
        order.status = OrderStatus.pending
        await session.commit()

        cart_text = format_cart(order)

    # Отправляем сообщение с реквизитами
    text = (
        f"✅ **Заказ #{order.id} оформлен!**\n\n"
        f"{cart_text}\n\n"
        f"💳 **Реквизиты для оплаты:**\n{PAYMENT_DETAILS}\n\n"
        "После оплаты нажмите кнопку ниже и пришлите фото чека."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Я оплатил — отправить чек", callback_data=f"payment:receipt:{order.id}")],
        [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"payment:cancel:{order.id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])
    await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
# --------------------------------------------------------


def register(app):
    app.add_handler(CallbackQueryHandler(view_cart, pattern='^cart:view$'))
    app.add_handler(CallbackQueryHandler(cart_remove_choose, pattern='^cart:remove:'))
    app.add_handler(CallbackQueryHandler(cart_delete_item, pattern='^cart:del_item:'))
    app.add_handler(CallbackQueryHandler(cart_edit_choose, pattern='^cart:edit:'))
    app.add_handler(CallbackQueryHandler(cart_change_qty_start, pattern='^cart:change_qty:'))
    app.add_handler(CallbackQueryHandler(cart_delta, pattern='^cart:delta:'))
    app.add_handler(CallbackQueryHandler(cart_input_start, pattern='^cart:input:'))
    app.add_handler(CallbackQueryHandler(cart_checkout, pattern='^cart:checkout:'))
    app.add_handler(CallbackQueryHandler(checkout_confirm, pattern='^checkout:confirm:'))