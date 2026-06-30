import io, logging
from datetime import datetime, timedelta, timezone
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from sqlalchemy import select, func, delete as sql_delete
from sqlalchemy.orm import selectinload
from bot.db import get_session, OrderStatus, Product, Order, OrderItem, User, get_all_users
from bot.db import set_bot_setting, get_bot_setting
from bot.db import invalidate_catalog_cache
from bot.config import ADMIN_USER_ID
from bot.keyboards import kb_admin_menu, kb_back_to_menu, kb_admin_confirm_payment, kb_admin_sync, kb_main_menu
from bot.excel_reports import build_monthly_report, build_clients_excel
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
    for idx, cat in enumerate(page_categories):
        # Сохраняем категорию в user_data по индексу и передаём только индекс
        context.user_data.setdefault('admin_cats', {})
        context.user_data['admin_cats'][idx] = cat
        kb.append([InlineKeyboardButton(cat, callback_data=f"admin:sc:{idx}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"admin:catlist:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"admin:catlist:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("🔍 Поиск по артикулу", callback_data="admin:search_article")])
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
async def show_stock_subcategories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    category = context.user_data.get('admin_current_cat')
    if not category:
        await safe_edit(query, "Сначала выберите категорию.", reply_markup=kb_back_to_menu())
        return

    async for session in get_session():
        products = (await session.execute(
            select(Product).where(Product.category == category)
        )).scalars().all()

    subcategories = {}
    for p in products:
        if ',' in p.name:
            sub = p.name.split(',')[0].strip()
        else:
            sub = p.name.strip()
        subcategories[sub] = subcategories.get(sub, 0) + 1

    if not subcategories:
        await safe_edit(query, f"В категории «{category}» нет товаров.", reply_markup=kb_back_to_menu())
        return

    all_subs = sorted(subcategories.keys())
    total = len(all_subs)
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_subs = all_subs[start:end]

    context.user_data['admin_subs'] = {idx: sub for idx, sub in enumerate(page_subs)}

    lines = [f"Подкатегории: {category} (стр. {page+1}/{total_pages})"]
    kb = []
    for idx, sub in enumerate(page_subs):
        kb.append([InlineKeyboardButton(sub, callback_data=f"admin:ss:{idx}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"admin:sublist:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"admin:sublist:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("↩️ К категориям", callback_data="admin:set_stock_list")])
    kb.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin:menu")])

    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
async def show_stock_products_page(query, context, category: str = None, page: int = 0, subcategory: str = None):
    if category is None:
        category = context.user_data.get('admin_current_cat')
    if subcategory is None:
        subcategory = context.user_data.get('admin_current_sub')
    context.user_data['admin_current_page'] = page

    if not category:
        await safe_edit(query, "Сначала выберите категорию.", reply_markup=kb_back_to_menu())
        return

    async for session in get_session():
        stmt = select(Product).where(Product.category == category)
        if subcategory:
            stmt = stmt.where(
                (Product.name == subcategory) |
                (Product.name.startswith(subcategory + ',')) |
		(Product.name.startswith(subcategory + ' ,'))
            )
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
        products = (await session.execute(
            stmt.order_by(Product.id).offset(page * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
        )).scalars().all()

    if not products:
        await safe_edit(query, f"Товары не найдены.", reply_markup=kb_back_to_menu())
        return

    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    cat_display = f"{category} / {subcategory}" if subcategory else category
    lines = [f"Остатки: {cat_display} (стр. {page+1}/{total_pages})"]
    kb = []

    for p in products:
        name_escaped = escape_markdown(p.name)   # экранируем название
        stock_str = f"{p.stock} шт." if p.stock is not None else "∞"
        status = "✅" if p.is_active else "❌ скрыт"
        lines.append(f"• {name_escaped} — на складе: {stock_str} {status}")
        kb.append([
            InlineKeyboardButton(f"✏️ {p.name[:25]}", callback_data=f"admin:set_stock_select:{p.id}"),
            InlineKeyboardButton("🗑", callback_data=f"admin:delete_prompt:{p.id}")
        ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"admin:prodpage:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"admin:prodpage:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)

    if subcategory:
        kb.append([InlineKeyboardButton("↩️ К подкатегориям", callback_data="admin:back_to_subs")])
    else:
        kb.append([InlineKeyboardButton("↩️ К категориям", callback_data="admin:set_stock_list")])
    kb.append([InlineKeyboardButton("⚙️ Админ‑меню", callback_data="admin:menu")])

    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
# ========== Обработчики команд ==========

async def back_to_subcategories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop('admin_current_sub', None)
    await show_stock_subcategories(update, context, page=0)

async def prodpage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":")[-1])
    await show_stock_products_page(query, context, page=page)
async def back_to_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «↩️ К списку товаров» (с экрана ввода остатка)."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    context.user_data.pop('state', None)
    ret = context.user_data.get('admin_return_context', {})
    category = ret.get('category')
    if not category:
        await safe_edit(query, "⚙️ **Админ‑меню:**", reply_markup=kb_admin_menu(),
                        parse_mode=ParseMode.MARKDOWN)
        return
    await show_stock_products_page(query, context, category=category,
                                   page=ret.get('page', 0), subcategory=ret.get('subcategory'))


async def admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Вход в админ-меню сбрасывает любой незавершённый текстовый ввод (FSM).
    context.user_data.pop('state', None)
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    await safe_edit(query, "⚙️ **Админ‑меню:**", reply_markup=kb_admin_menu(), parse_mode=ParseMode.MARKDOWN)


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
        user_info = escape_markdown(user_info)
        lines = [f"📦 **Заказ #{order.id}** — {user_info}"]
        for item in order.items:
            name = escape_markdown(item.product.name) if item.product else f"ID {item.product_id}"
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


# ========== Управление QR-кодом ==========

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
    # callback имеет формат "admin:sc:<индекс>"
    idx = int(query.data.split(":")[2])   # <-- исправлено: третий элемент
    cats = context.user_data.get('admin_cats', {})
    category = cats.get(idx)
    if not category:
        await query.answer("Категория не найдена.", show_alert=True)
        return
    context.user_data['admin_current_cat'] = category
    context.user_data.pop('admin_current_sub', None)
    await show_stock_subcategories(update, context, page=0)
async def stock_subcategory_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    # callback имеет формат "admin:ss:<индекс>"
    idx = int(query.data.split(":")[2])   # <-- исправлено
    subs = context.user_data.get('admin_subs', {})
    subcategory = subs.get(idx)
    if not subcategory:
        await query.answer("Подкатегория не найдена.", show_alert=True)
        return
    context.user_data['admin_current_sub'] = subcategory
    await show_stock_products_page(query, context, page=0)
async def set_stock_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    product_id = int(query.data.split(":")[-1])
    # Сохраняем контекст для возврата
    context.user_data['admin_return_context'] = {
        'category': context.user_data.get('admin_current_cat'),
        'subcategory': context.user_data.get('admin_current_sub'),
        'page': context.user_data.get('admin_current_page', 0)
    }
    context.user_data['state'] = 'admin_set_stock'
    context.user_data['data'] = {'product_id': product_id}
    # Контекст для кнопки «Назад к списку товаров».
    context.user_data['admin_return_context'] = {
        'category': context.user_data.get('admin_current_cat'),
        'subcategory': context.user_data.get('admin_current_sub'),
        'page': context.user_data.get('admin_current_page', 0),
    }
    back_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ К списку товаров", callback_data="admin:back_to_products")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")],
    ])
    # Отправляем запрос и сохраняем его message_id
    sent_msg = await query.message.reply_text(
        "✏️ Введите новое количество (целое число) или 0, чтобы скрыть товар:",
        reply_markup=back_kb
    )
    context.user_data['admin_stock_msg_id'] = sent_msg.message_id
    # Удаляем исходное сообщение с кнопкой (чтобы не мешало)
    try:
        await query.message.delete()
    except Exception:
        pass


async def delete_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    product_id = int(query.data.split(":")[-1])
    # Сохраняем место, куда вернуться после удаления (Баг 2).
    context.user_data['admin_delete_return'] = {
        'category': context.user_data.get('admin_current_cat'),
        'subcategory': context.user_data.get('admin_current_sub'),
        'page': context.user_data.get('admin_current_page', 0),
    }
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
    ret = context.user_data.get('admin_delete_return', {})
    category = ret.get('category')
    subcategory = ret.get('subcategory')
    page = ret.get('page', 0)

    async for session in get_session():
        product = await session.get(Product, product_id)
        if not product:
            await safe_edit(query, "❌ Товар не найден.", reply_markup=kb_admin_menu())
            return
        name = product.name
        await session.execute(sql_delete(OrderItem).where(OrderItem.product_id == product_id))
        await session.delete(product)
        await session.commit()
        invalidate_catalog_cache()
        logger.info("admin deleted product id=%s name=%s", product_id, name)

        # Сколько товаров осталось в этом разделе — чтобы скорректировать страницу.
        remaining = 0
        if category:
            cnt_stmt = select(Product).where(Product.category == category)
            if subcategory:
                cnt_stmt = cnt_stmt.where(
                    (Product.name == subcategory) | (Product.name.startswith(subcategory + ',')))
            remaining = (await session.execute(
                select(func.count()).select_from(cnt_stmt.subquery()))).scalar()

    # Баг 2: возвращаемся в список товаров, а не в главное меню.
    if not category:
        await safe_edit(query, f"✅ Товар «{name}» удалён.", reply_markup=kb_admin_menu())
        return
    # Если на текущей странице не осталось товаров — на предыдущую (не ниже 0).
    if remaining:
        total_pages = (remaining - 1) // ITEMS_PER_PAGE + 1
        page = max(0, min(page, total_pages - 1))
    else:
        page = 0
    await show_stock_products_page(query, context, category=category, page=page, subcategory=subcategory)


async def delete_by_articles_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    # Переводим бота в состояние ожидания артикулов
    context.user_data['state'] = 'admin_delete_by_articles'
    await safe_edit(query, "✏️ Введите артикулы через запятую (например, 2287, 2289, 2473):",
                    reply_markup=kb_back_to_menu())

async def cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "Удаление отменено.", reply_markup=kb_admin_menu())


async def admin_link_post_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает ручную привязку товара к посту канала (FSM)."""
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    context.user_data['state'] = 'admin_link_post'
    await safe_edit(
        query,
        "🔗 Введите через пробел: <код товара> <post_id>\n"
        "Например: 42 1287\n\n"
        "Код товара (product_id) виден в управлении остатками; "
        "post_id — это message_id поста в канале.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ В админ-меню", callback_data="admin:menu")]
        ]),
    )


# Поиск по артикулу в админке
async def admin_search_article_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        await query.answer("Нет доступа.", show_alert=True)
        return
    context.user_data['state'] = 'admin_search_article'
    await safe_edit(query, "🔎 Введите артикул для поиска:", reply_markup=kb_back_to_menu())


# ========== Регистрация обработчиков ==========

def register(app):
    # Админ-меню и отчёты
    app.add_handler(CallbackQueryHandler(admin_menu, pattern='^admin:menu$'))
    app.add_handler(CallbackQueryHandler(excel_monthly, pattern='^admin:excel:summary$'))
    app.add_handler(CallbackQueryHandler(excel_clients, pattern='^admin:excel:clients$'))
    app.add_handler(CallbackQueryHandler(confirm_list, pattern='^admin:confirm_list$'))

    # Ручная синхронизация
    app.add_handler(CallbackQueryHandler(sync_products, pattern='^admin:sync$'))
    app.add_handler(CallbackQueryHandler(sync_finish, pattern='^admin:sync:finish$'))

    # Управление QR-кодом
    app.add_handler(CallbackQueryHandler(admin_payment_qr_menu, pattern='^admin:payment_qr$'))
    app.add_handler(CallbackQueryHandler(admin_upload_qr_start, pattern='^admin:upload_qr$'))
    app.add_handler(CallbackQueryHandler(admin_show_qr, pattern='^admin:show_qr$'))
    app.add_handler(CallbackQueryHandler(admin_delete_qr, pattern='^admin:delete_qr$'))

    # Управление остатками – категории (первый уровень)
    app.add_handler(CallbackQueryHandler(set_stock_list, pattern='^admin:set_stock_list$'))
    app.add_handler(CallbackQueryHandler(
        lambda update, context: show_stock_categories(update, context,
                                                      page=int(update.callback_query.data.split(":")[-1])),
        pattern='^admin:catlist:'
    ))
    app.add_handler(CallbackQueryHandler(stock_category_page, pattern='^admin:sc:'))

    # Управление остатками – подкатегории (второй уровень)
    app.add_handler(CallbackQueryHandler(
        lambda update, context: show_stock_subcategories(update, context,
                                                         page=int(update.callback_query.data.split(":")[-1])),
        pattern='^admin:sublist:'
    ))
    app.add_handler(CallbackQueryHandler(stock_subcategory_page, pattern='^admin:ss:'))

    # Управление остатками – товары и навигация по страницам
    app.add_handler(CallbackQueryHandler(back_to_subcategories, pattern='^admin:back_to_subs$'))
    app.add_handler(CallbackQueryHandler(prodpage_handler, pattern='^admin:prodpage:'))

    # Изменение остатка конкретного товара
    app.add_handler(CallbackQueryHandler(set_stock_select, pattern='^admin:set_stock_select:'))
    app.add_handler(CallbackQueryHandler(back_to_products, pattern='^admin:back_to_products$'))

    # Удаление товара
    app.add_handler(CallbackQueryHandler(delete_prompt, pattern='^admin:delete_prompt:'))
    app.add_handler(CallbackQueryHandler(delete_confirm, pattern='^admin:delete_confirm:'))
    app.add_handler(CallbackQueryHandler(cancel_delete, pattern='^admin:cancel_delete$'))
    app.add_handler(CallbackQueryHandler(delete_by_articles_start, pattern='^admin:delete_by_articles$'))

    # Ручная привязка товара к посту канала
    app.add_handler(CallbackQueryHandler(admin_link_post_start, pattern='^admin:link_post$'))

    # Поиск по артикулу в админке
    app.add_handler(CallbackQueryHandler(admin_search_article_start, pattern='^admin:search_article$'))
