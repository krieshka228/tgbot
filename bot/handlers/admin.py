import io, logging
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from sqlalchemy import select, func, delete as sql_delete
from sqlalchemy.orm import selectinload
from bot.db import get_session, OrderStatus, Product, Order, OrderItem, User, get_all_users
from bot.config import ADMIN_USER_ID
from bot.keyboards import kb_admin_menu, kb_back_to_menu, kb_admin_confirm_payment, kb_admin_sync, kb_main_menu
from bot.excel_reports import build_monthly_report, build_clients_excel
from bot.db import set_bot_setting, get_bot_setting
from bot.utils import escape_markdown

logger = logging.getLogger(__name__)
ITEMS_PER_PAGE = 5


# ========== Вспомогательные функции ==========

async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.warning(f"Не удалось отредактировать сообщение: {e}")


async def show_stock_categories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    async for session in get_session():
        all_categories = (await session.execute(
            select(Product.category).where(Product.category != None).distinct().order_by(Product.category)
        )).scalars().all()

    if not all_categories:
        try:
            await query.edit_message_text("📭 В базе нет товаров.", reply_markup=kb_admin_menu())
        except Exception:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="📭 В базе нет товаров.",
                reply_markup=kb_admin_menu()
            )
        return

    total = len(all_categories)
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_categories = all_categories[start:end]

    lines = [f"**Категории** (стр. {page+1}/{total_pages})\n"]
    kb = []
    for cat in page_categories:
        kb.append([InlineKeyboardButton(cat, callback_data=f"admin:stock_category:{cat}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"admin:stock_catlist:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"admin:stock_catlist:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin:menu")])

    reply_markup = InlineKeyboardMarkup(kb)
    text = "\n".join(lines)

    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.MARKDOWN)
    except Exception:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.MARKDOWN
        )


async def show_stock_products_page(query, context, category: str, page: int):
    async for session in get_session():
        total = (await session.execute(
            select(func.count(Product.id)).where(Product.category == category)
        )).scalar()
        products = (await session.execute(
            select(Product).where(Product.category == category)
            .order_by(Product.id)
            .offset(page * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
        )).scalars().all()

    if not products:
        await safe_edit(query, f"В категории «{category}» нет товаров.", reply_markup=kb_back_to_menu())
        return

    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    lines = [f"**Остатки: {category}** (стр. {page+1}/{total_pages})\n"]
    kb = []

    for p in products:
        stock_str = f"{p.stock} шт." if p.stock is not None else "∞"
        status = "✅" if p.is_active else "❌ скрыт"
        name_escaped = escape_markdown(p.name)
        lines.append(f"• {name_escaped} — на складе: {stock_str} {status}")
        kb.append([
            InlineKeyboardButton(f"✏️ {p.name[:25]}", callback_data=f"admin:set_stock_select:{p.id}"),
            InlineKeyboardButton("🗑", callback_data=f"admin:delete_prompt:{p.id}")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"admin:stock_catpage:{category}:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"admin:stock_catpage:{category}:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("↩️ К категориям", callback_data="admin:set_stock_list")])
    kb.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin:menu")])

    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)


# ========== Обработчики команд ==========

async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await safe_edit(query, "⚙️ **Админ-меню:**", reply_markup=kb_admin_menu(), parse_mode=ParseMode.MARKDOWN)


async def excel_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer("Формирую отчёт...")
    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)
    async for session in get_session():
        stmt = select(Order).where(Order.created_at >= month_ago).options(
            selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.user)
        ).order_by(Order.created_at.desc())
        orders = (await session.execute(stmt)).scalars().all()
        if not orders:
            await safe_edit(query, "📊 За последний месяц нет заказов.", reply_markup=kb_admin_menu())
            return
        user_ids = {o.user_id for o in orders}
        users = (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    excel_bytes = build_monthly_report(orders, users)
    await query.message.reply_document(
        document=io.BytesIO(excel_bytes),
        filename="monthly_report.xlsx",
        caption=f"📊 Отчёт за месяц: {len(orders)} заказов, {len(users)} клиентов."
    )
    await safe_edit(query, "✅ Отчёт сформирован и отправлен файлом.")


async def excel_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await query.answer("Формирую базу клиентов...")
    async for session in get_session():
        users = await get_all_users(session)
        if not users:
            await safe_edit(query, "👥 База клиентов пуста.", reply_markup=kb_admin_menu())
            return
        excel_bytes = build_clients_excel(users)
    await query.message.reply_document(
        document=io.BytesIO(excel_bytes),
        filename="clients.xlsx",
        caption=f"👥 База клиентов ({len(users)} чел.) отправлена."
    )
    await safe_edit(query, "✅ База клиентов отправлена файлом.")


async def confirm_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    async for session in get_session():
        orders = (await session.execute(
            select(Order).where(Order.status == OrderStatus.paid)
            .options(selectinload(Order.items).selectinload(OrderItem.product), selectinload(Order.user))
        )).scalars().all()
    if not orders:
        await safe_edit(query, "✅ Нет заказов, ожидающих подтверждения.", reply_markup=kb_admin_menu())
        return
    for order in orders:
        user = order.user
        user_info = f"@{user.username}" if user and user.username else (user.full_name if user else f"ID {order.user_id}")
        lines = [f"📦 **Заказ #{order.id}** — {user_info}"]
        for item in order.items:
            name = item.product.name if item.product else f"ID {item.product_id}"
            lines.append(f"  • {name}: {item.quantity} шт. × {item.price_at_order:.0f} ₽")
        lines.append(f"💰 Итого: {order.total_amount:.0f} ₽")
        text = "\n".join(lines)
        await query.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_admin_confirm_payment(order.id)
        )
    await safe_edit(query, "✅ Список заказов к подтверждению выведен.", reply_markup=kb_admin_menu())


async def sync_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    context.user_data['state'] = 'admin_sync'
    context.user_data['sync_count'] = 0
    context.user_data['sync_skipped'] = 0
    await safe_edit(query,
        "📥 **Ручная синхронизация**\n\n"
        "Перешлите сюда посты из канала (можно несколько подряд). Бот обработает каждый.\n"
        "Для выхода нажмите кнопку «Завершить синхронизацию».",
        reply_markup=kb_admin_sync(), parse_mode=ParseMode.MARKDOWN
    )


async def sync_finish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    added = context.user_data.pop('sync_count', 0)
    skipped = context.user_data.pop('sync_skipped', 0)
    context.user_data.pop('state', None)
    text = "✅ Синхронизация завершена."
    if added:
        text += f"\nДобавлено товаров: {added}"
    if skipped:
        text += f"\nПропущено дубликатов: {skipped}"
    if not added and not skipped:
        text += "\nТовары не добавлены."
    await safe_edit(query, text, reply_markup=kb_main_menu(is_admin=True))


async def refresh_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    async for session in get_session():
        products = (await session.execute(
            select(Product).where(Product.is_active == True)
        )).scalars().all()
        updated = 0
        for product in products:
            if "," in product.name:
                new_cat = product.name.split(",")[0].strip()
            else:
                new_cat = product.name.strip()
            if product.category != new_cat:
                product.category = new_cat
                updated += 1
        await session.commit()
    await safe_edit(query, f"✅ Категории обновлены. Изменено товаров: {updated}", reply_markup=kb_admin_menu())


# ========== Управление QR-кодом оплаты ==========

async def admin_payment_qr_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🖼 Загрузить QR-код", callback_data="admin:upload_qr")],
        [InlineKeyboardButton("👀 Показать текущий", callback_data="admin:show_qr")],
        [InlineKeyboardButton("🗑 Удалить QR-код", callback_data="admin:delete_qr")],
        [InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin:menu")]
    ])
    await safe_edit(query, "**Управление QR-кодом для оплаты**", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def admin_upload_qr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    context.user_data['state'] = 'admin_payment_qr'
    await safe_edit(query, "📷 Пришлите PNG-изображение с QR-кодом:", reply_markup=kb_back_to_menu())


async def admin_show_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    async for session in get_session():
        token = await get_bot_setting(session, "payment_qr_token")
    if not token:
        await query.answer("QR-код не задан.", show_alert=True)
        return
    await context.bot.send_photo(chat_id=query.message.chat_id, photo=token)


async def admin_delete_qr(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    async for session in get_session():
        await set_bot_setting(session, "payment_qr_token", "")
    await query.answer("QR-код удалён.", show_alert=True)


# ========== Управление остатками ==========

async def set_stock_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await show_stock_categories(update, context, page=0)


async def stock_category_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    category = query.data.split(":", 2)[2]
    await show_stock_products_page(query, context, category, 0)


async def stock_catpage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    parts = query.data.split(":")
    category = parts[2]
    page = int(parts[3])
    await show_stock_products_page(query, context, category, page)


async def set_stock_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    product_id = int(query.data.split(":")[-1])
    context.user_data['state'] = 'admin_set_stock'
    context.user_data['data'] = {'product_id': product_id}
    await safe_edit(query, "✏️ Введите новое количество (целое число) или 0, чтобы скрыть товар:",
                    reply_markup=kb_back_to_menu())


async def delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    product_id = int(query.data.split(":")[-1])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, удалить", callback_data=f"admin:delete_confirm:{product_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="admin:cancel_delete")]
    ])
    await safe_edit(query, f"Удалить товар #{product_id}?", reply_markup=kb)


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    product_id = int(query.data.split(":")[-1])
    async for session in get_session():
        product = await session.get(Product, product_id)
        if product:
            await session.execute(
                sql_delete(OrderItem).where(OrderItem.product_id == product_id)
            )
            await session.delete(product)
            await session.commit()
            await safe_edit(query, f"✅ Товар «{product.name}» удалён.",
                            reply_markup=kb_main_menu(is_admin=True))
        else:
            await safe_edit(query, "❌ Товар не найден.",
                            reply_markup=kb_main_menu(is_admin=True))


async def cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "Удаление отменено.", reply_markup=kb_admin_menu())


def register(app):
    app.add_handler(CallbackQueryHandler(admin_menu, pattern='^admin:menu$'))
    app.add_handler(CallbackQueryHandler(excel_monthly, pattern='^admin:excel:summary$'))
    app.add_handler(CallbackQueryHandler(excel_clients, pattern='^admin:excel:clients$'))
    app.add_handler(CallbackQueryHandler(confirm_list, pattern='^admin:confirm_list$'))
    app.add_handler(CallbackQueryHandler(sync_products, pattern='^admin:sync$'))
    app.add_handler(CallbackQueryHandler(sync_finish, pattern='^admin:sync:finish$'))
    app.add_handler(CallbackQueryHandler(refresh_categories, pattern='^admin:refresh_categories$'))
    app.add_handler(CallbackQueryHandler(set_stock_list, pattern='^admin:set_stock_list$'))
    app.add_handler(CallbackQueryHandler(
        lambda update, context: show_stock_categories(update, context,
                                                      page=int(update.callback_query.data.split(":")[-1])),
        pattern='^admin:stock_catlist:'
    ))
    app.add_handler(CallbackQueryHandler(stock_category_page, pattern='^admin:stock_category:'))
    app.add_handler(CallbackQueryHandler(stock_catpage, pattern='^admin:stock_catpage:'))
    app.add_handler(CallbackQueryHandler(set_stock_select, pattern='^admin:set_stock_select:'))
    app.add_handler(CallbackQueryHandler(delete_prompt, pattern='^admin:delete_prompt:'))
    app.add_handler(CallbackQueryHandler(delete_confirm, pattern='^admin:delete_confirm:'))
    app.add_handler(CallbackQueryHandler(cancel_delete, pattern='^admin:cancel_delete$'))
    # Новые обработчики QR-кода
    app.add_handler(CallbackQueryHandler(admin_payment_qr_menu, pattern='^admin:payment_qr$'))
    app.add_handler(CallbackQueryHandler(admin_upload_qr_start, pattern='^admin:upload_qr$'))
    app.add_handler(CallbackQueryHandler(admin_show_qr, pattern='^admin:show_qr$'))
    app.add_handler(CallbackQueryHandler(admin_delete_qr, pattern='^admin:delete_qr$'))