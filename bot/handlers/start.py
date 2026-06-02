import logging
from datetime import datetime, timezone
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from bot.db import get_session, get_or_create_user
from bot.keyboards import kb_consent, kb_main_menu, kb_cart_actions, kb_back_to_menu, reply_main_menu
from bot.utils import parse_quantity, _parse_post_link, format_cart
from bot.config import ADMIN_USER_ID
from sqlalchemy import select
from bot.db import Product, get_or_create_draft, add_item_to_order, Order, OrderItem
from sqlalchemy.orm import selectinload
from bot.keyboards import kb_consent, kb_main_menu, kb_cart_actions, kb_back_to_menu, reply_main_menu

logger = logging.getLogger(__name__)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async for session in get_session():
        db_user = await get_or_create_user(
            session, user.id,
            full_name=user.full_name,
            username=user.username
        )
        if not db_user.consented:
            context.user_data['state'] = 'consent'
            await update.message.reply_text(
                "👋 Привет! Для продолжения нужно ваше согласие на обработку персональных данных.",
                reply_markup=kb_consent()
            )
        else:
            context.user_data.pop('state', None)
            await update.message.reply_text(
                "✅ Главное меню:",
                reply_markup=kb_main_menu(is_admin=(user.id == ADMIN_USER_ID))
            )


async def consent_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    async for session in get_session():
        db_user = await get_or_create_user(session, user.id)
        db_user.consented = True
        db_user.consented_at = datetime.now(timezone.utc)
        await session.commit()
    context.user_data.pop('state', None)
    await query.edit_message_text(
        "✅ Спасибо! Теперь вы можете делать заказы.",
        reply_markup=kb_main_menu(is_admin=(user.id == ADMIN_USER_ID))
    )

async def consent_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "❌ Без согласия на обработку данных работа с ботом невозможна.\n"
        "Если передумаете — нажмите /start",
        reply_markup=kb_back_to_menu()
    )

async def direct_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прямой заказ через личное сообщение (ссылка/артикул + количество)."""
    # Не срабатывает, если пользователь в FSM
    if context.user_data.get('state'):
        return
    message = update.message
    text = message.text.strip()
    user_id = message.from_user.id

    # Разбираем: первая часть — ссылка/число, вторая — количество
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return  # не формат заказа
    post_id = _parse_post_link(parts[0])
    qty = parse_quantity(parts[1])
    if post_id is None or qty is None:
        return

    async for session in get_session():
        user = await get_or_create_user(session, user_id)
        if not user.consented:
            await message.reply_text("❌ Сначала нужно дать согласие. Нажмите /start")
            return
        # Ищем товар
        stmt = select(Product).where(Product.post_id == post_id, Product.is_active == True)
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()
        if not product:
            await message.reply_text("⚠️ Товар с таким артикулом/постом не найден.")
            return
        order = await get_or_create_draft(session, user_id)
        stmt = select(Order).where(Order.id == order.id).options(selectinload(Order.items).selectinload(OrderItem.product))
        order = (await session.execute(stmt)).scalar_one()
        await add_item_to_order(session, order, product, qty)
        order = (await session.execute(stmt)).scalar_one()
        cart_text = format_cart(order)
    await message.reply_text(
        f"✅ **{product.name}** × {qty} шт. добавлен в корзину!\n\n{cart_text}",
        parse_mode="Markdown",
        reply_markup=kb_cart_actions(order.id)
    )


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('state', None)
    for msg_id in context.user_data.pop('catalog_messages', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass
    try:
        await query.message.delete()
    except Exception:
        pass
    is_admin = (query.from_user.id == ADMIN_USER_ID)
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text="🏠 Главное меню:",
        reply_markup=kb_main_menu(is_admin=is_admin)
    )


def register(app):
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CallbackQueryHandler(consent_yes, pattern='^consent:yes$'))
    app.add_handler(CallbackQueryHandler(consent_no, pattern='^consent:no$'))
    app.add_handler(CallbackQueryHandler(back_to_menu, pattern='^menu:main$'))