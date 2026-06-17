import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
from bot.db import get_session, get_or_create_user
from bot.keyboards import kb_consent, kb_main_menu, kb_back_to_menu
from bot.utils import escape_markdown
from bot.config import ADMIN_USER_ID
from bot.db import get_bot_setting, PendingOrder, Product

logger = logging.getLogger(__name__)


async def get_main_menu_info(is_admin: bool) -> tuple[str, InlineKeyboardMarkup]:
    if is_admin:
        return "⚙️ **Админ‑меню:**", kb_main_menu(is_admin=True)

    qr_available = False
    async for session in get_session():
        token = await get_bot_setting(session, "payment_qr_token")
        if token:
            qr_available = True
            break

    if qr_available:
        return "✅ Главное меню:", kb_main_menu(is_admin=False)
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✉️ Написать администратору", callback_data="contact:admin")],
            [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
        ])
        return "⚠️ Бот временно недоступен. Приносим извинения.", kb


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    async for session in get_session():
        db_user = await get_or_create_user(session, user_id,
                                           full_name=user.full_name,
                                           username=user.username)
        # Проверка отложенного заказа (если есть)
        pending = await session.get(PendingOrder, user_id)
        if pending:
            product = await session.get(Product, pending.product_id)
            if product and product.is_active:
                context.user_data['pending_order'] = {
                    'product_id': product.id,
                    'quantity': pending.quantity,
                    'product_name': product.name
                }
                context.user_data['state'] = 'confirm_pending_order'
                total = product.price * pending.quantity
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Подтвердить", callback_data=f"porder:confirm:{product.id}:{pending.quantity}")],
                    [InlineKeyboardButton("❌ Отменить", callback_data="porder:cancel")]
                ])
                await update.message.reply_text(
                    f"🛒 У вас есть неоформленный заказ:\n"
                    f"• {product.name} — {pending.quantity} шт. × {product.price:.0f} ₽ = {total:.0f} ₽\n\n"
                    f"Подтвердить?",
                    reply_markup=kb
                )
                return
            else:
                await session.delete(pending)
                await session.commit()

        # Удаляем старые сообщения с кнопками (если они остались в user_data)
        for msg_id in context.user_data.pop('catalog_messages', []):
            try:
                await context.bot.delete_message(chat_id=update.message.chat_id, message_id=msg_id)
            except Exception:
                pass

        # Определяем доступность QR и показываем актуальное меню
        is_admin = (user_id == ADMIN_USER_ID)
        text, kb = await get_main_menu_info(is_admin)
        await update.message.reply_text(text, reply_markup=kb)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('state', None)

    # Удаляем старые товары и навигацию каталога (если они остались)
    for msg_id in context.user_data.pop('catalog_product_msgs', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass
    nav_msg_id = context.user_data.pop('catalog_nav_msg_id', None)
    if nav_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=nav_msg_id)
        except Exception:
            pass

    is_admin = (query.from_user.id == ADMIN_USER_ID)
    text, kb = await get_main_menu_info(is_admin)

    # Проверяем, не совпадает ли новое содержимое с текущим
    current_text = query.message.text or query.message.caption
    current_markup = query.message.reply_markup
    if current_text == text and current_markup == kb:
        # Содержимое идентично – просто подтверждаем callback без действий
        return

    # Пытаемся отредактировать текущее сообщение
    try:
        await query.edit_message_text(text=text, reply_markup=kb)
    except Exception:
        # Не удалось отредактировать – удаляем текущее и отправляем новое
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=kb
        )
def register(app):
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^menu:main$'))