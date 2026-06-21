import logging
from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from bot.db import get_session, get_order_with_items, OrderStatus, invalidate_catalog_cache
from bot.keyboards import kb_payment, kb_admin_confirm_payment, kb_main_menu, kb_back_to_menu
from bot.config import ADMIN_USER_ID, ADMIN_CHAT_ID
from bot.utils import format_order_for_admin, format_cart
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


async def payment_receipt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":")[-1])
    context.user_data['state'] = 'awaiting_receipt'
    context.user_data['data'] = {'order_id': order_id}
    # Безопасное редактирование: если сообщение не текстовое, удаляем и отправляем новое
    try:
        await query.edit_message_text("📷 Пришлите фото или скриншот чека об оплате:")
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📷 Пришлите фото или скриншот чека об оплате:"
        )


async def payment_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = int(query.data.split(":")[-1])
    user_id = query.from_user.id

    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await query.edit_message_text("❌ Заказ не найден.")
            return

        # Запрещаем отмену для оплаченных/подтверждённых заказов
        if order.status == OrderStatus.paid:
            await query.edit_message_text(
                "⚠️ Заказ уже оплачен, отмена невозможна.",
                reply_markup=kb_back_to_menu()
            )
            return

        if order.status == OrderStatus.confirmed:
            await query.edit_message_text(
                "⚠️ Заказ уже подтверждён, отмена невозможна.",
                reply_markup=kb_back_to_menu()
            )
            return

        # Отмена только для pending заказов младше 5 минут
        if order.status == OrderStatus.pending:
            created_at = order.created_at.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_seconds = (now - created_at).total_seconds()
            if age_seconds >= 300:
                await query.edit_message_text(
                    "⚠️ С момента оформления заказа прошло более 5 минут. Отмена невозможна.",
                    reply_markup=kb_back_to_menu()
                )
                return

            # Отмена: возвращаем остатки
            for item in order.items:
                if item.product and item.product.stock is not None:
                    item.product.stock += item.quantity
                    item.product.is_active = item.product.stock > 0
                    item.product.in_stock = item.product.stock > 0
            order.status = OrderStatus.cancelled
            await session.commit()
            invalidate_catalog_cache()
            fio = order.full_name or (order.user.full_name if order.user else None)
            logger.info("order cancelled by client",
                        extra={"event": "order_cancelled", "user_id": user_id,
                               "order_id": order_id})
            # Уведомляем администратора
            admin_text = f"❌ Клиент отменил заказ #{order_id} на {order.total_amount:.0f} ₽."
            if fio:
                admin_text += f"\nФИО: {fio}"
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text)
            except Exception as e:
                logger.warning(f"Не удалось уведомить администратора об отмене: {e}")

            # Отправляем подтверждение клиенту — безопасно, удаляя старое сообщение
            try:
                await query.edit_message_text(f"❌ Заказ #{order_id} отменён.", reply_markup=kb_back_to_menu())
            except Exception:
                # Если не удалось отредактировать (например, сообщение с фото), удаляем и отправляем новое
                try:
                    await query.message.delete()
                except Exception:
                    pass
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=f"❌ Заказ #{order_id} отменён.",
                    reply_markup=kb_back_to_menu()
                )
        else:
            await query.edit_message_text(
                "⚠️ Отмена заказа невозможна.",
                reply_markup=kb_back_to_menu()
            )


async def handle_receipt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.from_user:
        return
    if context.user_data is None:
        return
    if context.user_data.get('state') != 'awaiting_receipt':
        return

    message = update.message
    user_id = message.from_user.id
    data = context.user_data.get('data', {})
    order_id = data.get('order_id')

    photo = message.photo[-1] if message.photo else None
    if not photo:
        await message.reply_text("📷 Пожалуйста, пришлите именно фото чека (изображение).")
        return

    file_id = photo.file_id
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await message.reply_text("❌ Заказ не найден.")
            context.user_data.pop('state', None)
            return
        order.status = OrderStatus.paid
        order.receipt_file_id = file_id
        await session.commit()
        order_info = format_order_for_admin(order)

    try:
        await context.bot.send_photo(
            chat_id=ADMIN_CHAT_ID,
            photo=file_id,
            caption=f"💳 **Новый чек об оплате!**\n\n{order_info}\n\nПроверьте оплату:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_confirm_payment(order_id)
        )
    except Exception as e:
        logger.error(f"Не удалось уведомить администратора: {e}")

    context.user_data.pop('state', None)
    await message.reply_text(
        f"✅ Чек получен! Ожидайте подтверждения оплаты по заказу #{order_id}.",
        reply_markup=kb_main_menu(is_admin=(user_id == ADMIN_USER_ID))
    )


async def admin_pay_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    order_id = int(query.data.split(":")[-1])
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order:
            await query.edit_message_text("❌ Заказ не найден.")
            return
        order.status = OrderStatus.confirmed
        # Только обновляем активность товаров, НЕ списываем остатки повторно!
        for item in order.items:
            product = item.product
            if product:
                product.is_active = (product.stock is not None and product.stock > 0)
                product.in_stock = product.is_active
        await session.commit()
        invalidate_catalog_cache()
        client_id = order.user_id

    # Просим клиента ввести телефон
    await context.bot.send_message(
        chat_id=client_id,
        text="📱 Введите ваш номер телефона для связи:"
    )
    # Безопасное уведомление администратора
    try:
        await query.edit_message_text(f"✅ Оплата заказа #{order_id} подтверждена. Ожидаем телефон от клиента.")
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"✅ Оплата заказа #{order_id} подтверждена. Ожидаем телефон от клиента."
        )
async def admin_pay_fail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    order_id = int(query.data.split(":")[-1])
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order:
            await query.edit_message_text("❌ Заказ не найден.")
            return
        order.status = OrderStatus.pending
        await session.commit()
        client_id = order.user_id
    await context.bot.send_message(
        chat_id=client_id,
        text=f"❌ Оплата заказа #{order_id} не подтверждена.\n"
             "Проверьте реквизиты и попробуйте снова или напишите администратору.",
        reply_markup=kb_payment(order_id)
    )
    # Безопасное уведомление админа
    try:
        await query.edit_message_text(f"❌ Оплата заказа #{order_id} отклонена.")
    except Exception:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"❌ Оплата заказа #{order_id} отклонена."
        )

def register(app):
    app.add_handler(CallbackQueryHandler(payment_receipt_start, pattern='^payment:receipt:'))
    app.add_handler(CallbackQueryHandler(payment_cancel, pattern='^payment:cancel:'))
    app.add_handler(CallbackQueryHandler(admin_pay_ok, pattern='^admin:pay_ok:'))
    app.add_handler(CallbackQueryHandler(admin_pay_fail, pattern='^admin:pay_fail:'))
    # block=False разрешает обработку сообщения другими обработчиками, если это не чек
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, handle_receipt, block=False), group=1)