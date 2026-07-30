"""
handlers/cart.py — корзина, оформление заказа.
"""
import asyncio
import logging

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.db import (
    get_session,
    get_or_create_user,
    get_draft_order,
    remove_item_from_order,
    recalculate_total,
    OrderStatus,
    Order,
    OrderItem,
    Product,
    User,  # <-- ДОБАВИТЬ
    get_bot_setting,
    invalidate_catalog_cache,
)
from bot.keyboards import kb_cart_actions, kb_cart_items_remove, kb_back_to_menu, kb_main_menu
from bot.utils import format_cart, escape_markdown
from bot.config import ADMIN_USER_ID

logger = logging.getLogger(__name__)


async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр текущей корзины (редактирует текущее сообщение)."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_draft_order(session, user_id)

    if order is None or not order.items:
        await query.edit_message_text(
            "🛒 Ваша корзина пуста.\n\nПерейдите в каталог и добавьте товары.",
            reply_markup=kb_back_to_menu()
        )
        return

    await query.edit_message_text(
        format_cart(order),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_cart_actions(order.id, has_items=True)
    )


async def cart_remove_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список позиций для удаления."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_draft_order(session, user_id)

    if not order or not order.items:
        await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
        return

    await query.edit_message_text(
        "Выберите позицию для удаления:",
        reply_markup=kb_cart_items_remove(order)
    )


async def cart_delete_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет выбранный товар из корзины."""
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_draft_order(session, user_id)
        if not order:
            await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
            return

        removed = await remove_item_from_order(session, order, item_id)
        if removed:
            await session.refresh(order)
            if order.items:
                await query.edit_message_text(
                    "✅ Удалено.\n\n" + format_cart(order),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=kb_cart_actions(order.id)
                )
            else:
                await query.edit_message_text(
                    "✅ Удалено. Корзина пуста.",
                    reply_markup=kb_back_to_menu()
                )
        else:
            await query.edit_message_text("❌ Позиция не найдена.", reply_markup=kb_back_to_menu())


async def cart_edit_choose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор товара для изменения количества."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_draft_order(session, user_id)

    if not order or not order.items:
        await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
        return

    buttons = []
    for item in order.items:
        name = item.product.name if item.product else f"Товар #{item.product_id}"
        buttons.append([InlineKeyboardButton(f"{name} (x{item.quantity})", callback_data=f"cart:change_qty:{item.id}")])
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="cart:view")])

    await query.edit_message_text(
        "Выберите позицию для изменения:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cart_change_qty_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает интерфейс изменения количества (шаг +/− или ввод)."""
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_draft_order(session, user_id)

    if not order:
        await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
        return

    item = next((i for i in order.items if i.id == item_id), None)
    if not item:
        await query.edit_message_text("❌ Позиция не найдена.", reply_markup=kb_back_to_menu())
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

    await query.edit_message_text(
        f"Количество: **{current_qty}**\nВыберите действие:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


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
            await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
            return

        item = next((i for i in order.items if i.id == item_id), None)
        if not item:
            await query.edit_message_text("❌ Позиция не найдена.", reply_markup=kb_back_to_menu())
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

    # После изменения показываем корзину
    async for session in get_session():
        order = await get_draft_order(session, user_id)

    if not order or not order.items:
        await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
        return

    await query.edit_message_text(
        format_cart(order),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_cart_actions(order.id)
    )


async def cart_input_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переводит в режим ручного ввода количества."""
    query = update.callback_query
    await query.answer()
    item_id = int(query.data.split(":")[-1])
    context.user_data['state'] = 'cart_change_qty'
    context.user_data['data'] = {'item_id': item_id}

    await query.edit_message_text(
        "✏️ Введите новое количество (целое число):",
        reply_markup=kb_back_to_menu()
    )


async def cart_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает сводку заказа с кнопками «Подтвердить» и «Изменить»."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_draft_order(session, user_id)
        if not order or not order.items:
            await query.edit_message_text("🛒 Корзина пуста.", reply_markup=kb_back_to_menu())
            return

    cart_text = format_cart(order)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить заказ", callback_data=f"checkout:confirm:{order.id}")],
        [InlineKeyboardButton("✏️ Изменить заказ", callback_data="cart:view")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])

    await query.edit_message_text(
        f"📋 **Проверьте ваш заказ:**\n\n{cart_text}\n\nВсё верно?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb
    )


async def process_checkout_with_bonus(query, context, order_id: int, user_id: int, bonus_amount: int):
    """Оформление заказа с учётом бонусов (TG-бот)."""
    try:
        qr_telegram = None
        async for session in get_session():
            qr_telegram = await get_bot_setting(session, "payment_qr_telegram")
            break

        if not qr_telegram:
            await query.edit_message_text(
                "⚠️ QR-код не загружен. Обратитесь к администратору.",
                reply_markup=kb_back_to_menu()
            )
            return

        async for session in get_session():
            stmt = (
                select(Order)
                .where(Order.id == order_id, Order.user_id == user_id)
                .options(selectinload(Order.items).selectinload(OrderItem.product))
                .with_for_update()
            )
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()

            if not order:
                await query.edit_message_text("❌ Заказ не найден.", reply_markup=kb_back_to_menu())
                return

            if order.status != OrderStatus.draft:
                # статус уже изменён
                return

            # Списываем бонусы с TG-баланса
            user = await session.get(User, user_id)
            if user and bonus_amount > 0:
                user.bonus_balance_tg = (user.bonus_balance_tg or 0) - bonus_amount
                order.total_amount -= bonus_amount
                order.bonus_used = bonus_amount

            order.status = OrderStatus.pending
            await session.commit()
            invalidate_catalog_cache()
            cart_text = format_cart(order)

        bonus_text = f"\n💎 Списано бонусов: {bonus_amount}" if bonus_amount > 0 else ""
        text = (
            f"✅ **Заказ #{order.id} оформлен!**\n\n"
            f"{cart_text}\n"
            f"{bonus_text}\n\n"
            "После оплаты нажмите кнопку ниже и пришлите фото чека."
        )

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Я оплатил — отправить чек", callback_data=f"payment:receipt:{order.id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"payment:cancel:{order.id}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
        ])

        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=qr_telegram,
            caption=text,
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            await query.message.delete()
        except Exception:
            pass

        logger.info("order confirmed with bonus",
                    extra={"event": "order_confirmed", "user_id": user_id, "order_id": order.id,
                           "bonus_used": bonus_amount})

    except Exception as e:
        logger.exception(f"Ошибка при подтверждении заказа: {e}")
        await query.edit_message_text(
            "⚠️ Произошла ошибка при оформлении заказа. Попробуйте ещё раз.",
            reply_markup=kb_back_to_menu()
        )


async def checkout_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Финальное подтверждение заказа с атомарным резервированием и показом QR-кода (TG-бот)."""
    query = update.callback_query
    await query.answer()

    order_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    try:
        # 1. Проверка бонусов (используем TG-баланс)
        bonus_balance = 0
        order_total = 0
        async for session in get_session():
            user = await session.get(User, user_id)
            if user:
                bonus_balance = user.bonus_balance_tg or 0

            order = await get_draft_order(session, user_id)
            if order:
                order_total = order.total_amount

            break

        # Если есть бонусы — запрашиваем сумму списания
        if bonus_balance > 0 and order_total > 0:
            context.user_data['state'] = 'order_bonus_input'
            context.user_data['data'] = {
                'order_id': order_id,
                'bonus_balance': bonus_balance,      # в process_order_bonus_input используется это имя
                'order_total': order_total,
            }
            max_bonus = min(bonus_balance, int(order_total * 0.2))
            await query.edit_message_text(
                f"💎 **У вас {bonus_balance} бонусов!**\n\n"
                f"Вы можете оплатить до 20% стоимости заказа.\n"
                f"💰 Максимум: {max_bonus} бонусов.\n\n"
                f"Введите сумму бонусов для списания (или 0, чтобы не использовать):",
                reply_markup=kb_back_to_menu()
            )
            return

        # 2. Получаем QR-код
        qr_telegram = None
        for attempt in range(3):
            try:
                async for session in get_session():
                    qr_telegram = await get_bot_setting(session, "payment_qr_telegram")
                if qr_telegram:
                    break
            except Exception as e:
                logger.warning(f"QR получение, попытка {attempt + 1}: {e}")
                await asyncio.sleep(0.5)

        if not qr_telegram:
            await query.edit_message_text(
                "⚠️ QR-код не загружен. Обратитесь к администратору.",
                reply_markup=kb_back_to_menu()
            )
            return

        # 3. Обработка заказа
        async for session in get_session():
            stmt = (
                select(Order)
                .where(Order.id == order_id, Order.user_id == user_id)
                .options(selectinload(Order.items).selectinload(OrderItem.product))
                .with_for_update()
            )
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()

            if not order:
                await query.edit_message_text("❌ Заказ не найден.", reply_markup=kb_back_to_menu())
                return

            if order.status != OrderStatus.draft:
                if order.status == OrderStatus.pending:
                    text = f"ℹ️ Заказ #{order.id} уже оформлен и ожидает оплаты."
                    kb = InlineKeyboardMarkup([
                        [InlineKeyboardButton("💳 Оплатить", callback_data=f"payment:receipt:{order.id}")],
                        [InlineKeyboardButton("❌ Отменить", callback_data=f"payment:cancel:{order.id}")],
                        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
                    ])
                    await query.edit_message_text(text, reply_markup=kb)
                else:
                    await query.edit_message_text(
                        f"ℹ️ Заказ #{order.id} уже имеет статус {order.status.value}.",
                        reply_markup=kb_back_to_menu()
                    )
                return

            # Проверка остатков (оставим без изменений, но stock не используется)
            product_ids = [item.product_id for item in order.items]
            if product_ids:
                lock_products = select(Product).where(Product.id.in_(product_ids)).with_for_update()
                locked = (await session.execute(lock_products)).scalars().all()
                product_map = {p.id: p for p in locked}

            order.status = OrderStatus.pending
            await session.commit()
            invalidate_catalog_cache()
            cart_text = format_cart(order)

        # 4. Отправка фото с QR
        text = (
            f"✅ **Заказ #{order.id} оформлен!**\n\n"
            f"{cart_text}\n\n"
            "После оплаты нажмите кнопку ниже и пришлите фото чека."
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Я оплатил — отправить чек", callback_data=f"payment:receipt:{order.id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"payment:cancel:{order.id}")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
        ])

        for attempt in range(3):
            try:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id,
                    photo=qr_telegram,
                    caption=text,
                    reply_markup=kb,
                    parse_mode=ParseMode.MARKDOWN
                )
                break
            except Exception as e:
                logger.warning(f"Отправка QR фото, попытка {attempt + 1}: {e}")
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)

        try:
            await query.message.delete()
        except Exception:
            pass

        logger.info("order confirmed", extra={"event": "order_confirmed", "user_id": user_id, "order_id": order.id})

    except Exception as e:
        logger.exception(f"Ошибка при подтверждении заказа: {e}")
        await query.edit_message_text(
            "⚠️ Произошла ошибка при оформлении заказа. Попробуйте ещё раз или обратитесь к администратору.",
            reply_markup=kb_back_to_menu()
        )


def register(app):
    """Регистрирует обработчики корзины."""
    app.add_handler(CallbackQueryHandler(view_cart, pattern='^cart:view$'))
    app.add_handler(CallbackQueryHandler(cart_remove_choose, pattern='^cart:remove:'))
    app.add_handler(CallbackQueryHandler(cart_delete_item, pattern='^cart:del_item:'))
    app.add_handler(CallbackQueryHandler(cart_edit_choose, pattern='^cart:edit:'))
    app.add_handler(CallbackQueryHandler(cart_change_qty_start, pattern='^cart:change_qty:'))
    app.add_handler(CallbackQueryHandler(cart_delta, pattern='^cart:delta:'))
    app.add_handler(CallbackQueryHandler(cart_input_start, pattern='^cart:input:'))
    app.add_handler(CallbackQueryHandler(cart_checkout, pattern='^cart:checkout:'))
    app.add_handler(CallbackQueryHandler(checkout_confirm, pattern='^checkout:confirm:'))