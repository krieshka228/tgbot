import logging
from urllib.parse import quote, unquote
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import ContextTypes, CallbackQueryHandler
from telegram.constants import ParseMode
from sqlalchemy import select, func
from bot.db import get_session, Product, get_or_create_user, get_or_create_draft, add_item_to_order, Order, OrderItem
from sqlalchemy.orm import selectinload
from bot.keyboards import kb_cart_actions, kb_back_to_menu
from bot.utils import format_cart, parse_quantity, escape_markdown
from bot.config import ADMIN_USER_ID
from bot.db import get_bot_setting

logger = logging.getLogger(__name__)
ITEMS_PER_PAGE = 3

_catalog_messages: dict[int, list[str]] = {}


async def delete_catalog_messages(user_id: int, bot, also_delete_message_id: str | None = None):
    ids_to_delete = _catalog_messages.pop(user_id, [])[:]
    if also_delete_message_id and also_delete_message_id not in ids_to_delete:
        ids_to_delete.append(also_delete_message_id)
    for mid in ids_to_delete:
        try:
            await bot.delete_message(mid)
        except Exception:
            pass


# ================== УРОВЕНЬ 1: категории ==================
async def catalog_show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = update.callback_query
    await query.answer()

    # Очищаем старые сообщения каталога
    for msg_id in context.user_data.pop('catalog_messages', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass
    try:
        await query.message.delete()
    except Exception:
        pass

    async for session in get_session():
        all_categories = (await session.execute(
            select(Product.category).where(Product.is_active == True, Product.category != None).distinct().order_by(Product.category)
        )).scalars().all()

    if not all_categories:
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="📭 В каталоге пока нет категорий.",
            reply_markup=kb_back_to_menu()
        )
        context.user_data['catalog_messages'] = [msg.message_id]
        return

    total = len(all_categories)
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_categories = all_categories[start:end]

    # Сохраняем категории в user_data по индексам, чтобы передавать только индекс в callback
    context.user_data['catalog_cats'] = {idx: cat for idx, cat in enumerate(page_categories)}

    kb = []
    for idx, cat in enumerate(page_categories):
        kb.append([InlineKeyboardButton(cat, callback_data=f"catalog:sc:{idx}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"catalog:catlist:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"catalog:catlist:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])

    nav_text = f"**Категории** (стр. {page+1}/{total_pages})"
    msg = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=nav_text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data['catalog_messages'] = [msg.message_id]


# ================== УРОВЕНЬ 2: подкатегории ==================
async def catalog_show_subcategories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Показывает подкатегории для ранее выбранной категории (из user_data)."""
    query = update.callback_query
    await query.answer()

    category = context.user_data.get('catalog_current_cat')
    if not category:
        # Если категория не выбрана (например, при прямом вызове), возвращаемся в категории
        await catalog_show_categories(update, context)
        return

    # Очищаем старые сообщения
    for msg_id in context.user_data.pop('catalog_messages', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass
    try:
        await query.message.delete()
    except Exception:
        pass

    async for session in get_session():
        products = (await session.execute(
            select(Product).where(Product.is_active == True, Product.category == category)
        )).scalars().all()

    # Формируем список подкатегорий (часть названия до первой запятой)
    subcategories = {}
    for p in products:
        if ',' in p.name:
            sub = p.name.split(',')[0].strip()
        else:
            sub = p.name.strip()
        subcategories[sub] = subcategories.get(sub, 0) + 1

    if not subcategories:
        msg = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"В категории «{category}» пока нет товаров.",
            reply_markup=kb_back_to_menu()
        )
        context.user_data['catalog_messages'] = [msg.message_id]
        return

    all_subs = sorted(subcategories.keys())
    total = len(all_subs)
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_subs = all_subs[start:end]

    # Сохраняем подкатегории в user_data по индексам
    context.user_data['catalog_subs'] = {idx: sub for idx, sub in enumerate(page_subs)}

    kb = []
    for idx, sub in enumerate(page_subs):
        kb.append([InlineKeyboardButton(sub, callback_data=f"catalog:ss:{idx}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"catalog:sublist:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"catalog:sublist:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")])
    kb.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])

    msg = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=f"**{category}** — выберите подкатегорию:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data['catalog_messages'] = [msg.message_id]


# ================== ТОВАРЫ ==================
async def show_products_page(query, context, page: int = 0):
    """Показывает товары внутри выбранной подкатегории (данные берутся из user_data)."""
    category = context.user_data.get('catalog_current_cat')
    subcategory = context.user_data.get('catalog_current_sub')
    if not category:
        await query.edit_message_text("Сначала выберите категорию.", reply_markup=kb_back_to_menu())
        return

    async for session in get_session():
        stmt = select(Product).where(Product.is_active == True, Product.category == category)
        if subcategory:
            stmt = stmt.where(
                (Product.name == subcategory) |
                (Product.name.startswith(subcategory + ',')) |
                (Product.name.startswith(subcategory + ' '))
            )
        total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar()
        products = (await session.execute(
            stmt.order_by(Product.id).offset(page * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
        )).scalars().all()

    if not products:
        await query.edit_message_text(f"Товары не найдены.", reply_markup=kb_back_to_menu())
        return

    new_msgs = []
    chat_id = query.message.chat_id
    bot = context.bot

    for product in products:
        name = escape_markdown(product.name)
        article = escape_markdown(product.article) if product.article else None
        text = name
        if article:
            text += f"\nАртикул {article}"
        if product.stock is not None:
            text += f"\nНа складе: {product.stock}"
        text += f"\n\nЦена {product.price:.0f} ₽"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Заказать", callback_data=f"order:start:{product.id}")]])

        photos = product.photo_file_ids.split(',') if product.photo_file_ids else []
        videos = product.video_file_ids.split(',') if product.video_file_ids else []

        try:
            if len(photos) == 1 and not videos:
                msg = await bot.send_photo(chat_id=chat_id, photo=photos[0], caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                new_msgs.append(msg.message_id)
            elif len(videos) == 1 and not photos:
                msg = await bot.send_video(chat_id=chat_id, video=videos[0], caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                new_msgs.append(msg.message_id)
            elif photos or videos:
                media = []
                for idx, fid in enumerate(photos):
                    if idx == 0 and not videos:
                        media.append(InputMediaPhoto(media=fid, caption=text, parse_mode=ParseMode.MARKDOWN))
                    else:
                        media.append(InputMediaPhoto(media=fid))
                for idx, fid in enumerate(videos):
                    if idx == 0 and not photos:
                        media.append(InputMediaVideo(media=fid, caption=text, parse_mode=ParseMode.MARKDOWN))
                    else:
                        media.append(InputMediaVideo(media=fid))
                if photos and videos:
                    media[0] = InputMediaPhoto(media=photos[0], caption=text, parse_mode=ParseMode.MARKDOWN)
                msgs = await bot.send_media_group(chat_id=chat_id, media=media)
                for m in msgs:
                    new_msgs.append(m.message_id)
                btn_msg = await bot.send_message(chat_id=chat_id, text="Выберите действие:", reply_markup=kb)
                new_msgs.append(btn_msg.message_id)
            else:
                msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                new_msgs.append(msg.message_id)
        except Exception as e:
            logger.warning(f"Ошибка отправки медиа для товара {product.id}: {e}")
            msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            new_msgs.append(msg.message_id)

    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"catalog:prodpage:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд →", callback_data=f"catalog:prodpage:{page+1}"))
    kb_nav = []
    if nav:
        kb_nav.append(nav)
    if subcategory:
        kb_nav.append([InlineKeyboardButton("↩️ К подкатегориям", callback_data="catalog:back_to_subs")])
    else:
        kb_nav.append([InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")])
    kb_nav.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    nav_text = f"**{category} / {subcategory or 'все'}** (стр. {page+1}/{total_pages})"
    msg_nav = await bot.send_message(chat_id=chat_id, text=nav_text,
                                      reply_markup=InlineKeyboardMarkup(kb_nav), parse_mode=ParseMode.MARKDOWN)
    new_msgs.append(msg_nav.message_id)
    context.user_data['catalog_messages'] = new_msgs
    try:
        await query.message.delete()
    except Exception:
        pass


# ================== ЗАКАЗ ==================
async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Первый ответ обязателен для Telegram
    await query.answer()

    logger.info("start_order вызван!")

    # Проверка доступности QR для ВСЕХ пользователей
    async for session in get_session():
        token = await get_bot_setting(session, "payment_qr_token")
        logger.info(f"QR token: {token}")
        if not token:
            if query.from_user.id == ADMIN_USER_ID:
                await query.answer("⚠️ QR-код не задан. Загрузите его в админ‑меню.", show_alert=True)
            else:
                await query.answer("⚠️ Бот временно недоступен.", show_alert=True)
            return
        break   # токен есть

    product_id = int(query.data.split(":")[-1])
    logger.info(f"product_id={product_id}")

    try:
        async for session in get_session():
            product = await session.get(Product, product_id)
            break
        if not product or not product.is_active:
            logger.warning(f"Товар не найден или неактивен: {product_id}")
            await query.answer("Товар недоступен.", show_alert=True)
            return

        logger.info("Товар найден, удаляю сообщение...")
        await query.message.delete()

        # Экранирование
        name = escape_markdown(product.name)
        article = escape_markdown(product.article) if product.article else None
        text = name
        if article:
            text += f"\nАртикул {article}"
        if product.stock is not None:
            text += f"\nНа складе: {product.stock}"
        text += f"\n\nЦена {product.price:.0f} ₽\n\n✏️ Введите количество:"

        photos = product.photo_file_ids.split(',') if product.photo_file_ids else []
        videos = product.video_file_ids.split(',') if product.video_file_ids else []

        card_msg_id = None
        try:
            if len(photos) == 1 and not videos:
                msg = await query.message.reply_photo(
                    photo=photos[0],
                    caption=text,
                    reply_markup=kb_back_to_menu(),
                    parse_mode=ParseMode.MARKDOWN
                )
                card_msg_id = msg.message_id
            elif len(videos) == 1 and not photos:
                msg = await query.message.reply_video(
                    video=videos[0],
                    caption=text,
                    reply_markup=kb_back_to_menu(),
                    parse_mode=ParseMode.MARKDOWN
                )
                card_msg_id = msg.message_id
            elif photos or videos:
                media = []
                for idx, fid in enumerate(photos):
                    if idx == 0 and not videos:
                        media.append(InputMediaPhoto(media=fid, caption=text, parse_mode=ParseMode.MARKDOWN))
                    else:
                        media.append(InputMediaPhoto(media=fid))
                for idx, fid in enumerate(videos):
                    if idx == 0 and not photos:
                        media.append(InputMediaVideo(media=fid, caption=text, parse_mode=ParseMode.MARKDOWN))
                    else:
                        media.append(InputMediaVideo(media=fid))
                if photos and videos:
                    media[0] = InputMediaPhoto(media=photos[0], caption=text, parse_mode=ParseMode.MARKDOWN)
                msgs = await query.message.reply_media_group(media=media)
                btn_msg = await query.message.reply_text("Выберите действие:", reply_markup=kb_back_to_menu())
                card_msg_id = btn_msg.message_id
            else:
                msg = await query.message.reply_text(text, reply_markup=kb_back_to_menu(), parse_mode=ParseMode.MARKDOWN)
                card_msg_id = msg.message_id
        except Exception as e:
            logger.error(f"Ошибка отправки медиа: {e}", exc_info=True)
            msg = await query.message.reply_text(text, reply_markup=kb_back_to_menu(), parse_mode=ParseMode.MARKDOWN)
            card_msg_id = msg.message_id

        context.user_data['state'] = 'order_qty'
        context.user_data['data'] = {'product_id': product_id, 'card_msg_id': card_msg_id}
        logger.info("Запрос количества отправлен")

    except Exception as e:
        logger.error(f"Ошибка в start_order: {e}", exc_info=True)
        await query.answer("Произошла ошибка. Попробуйте позже.", show_alert=True)


# ================== ПОИСК ПО НАЗВАНИЮ ==================
async def search_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'search_name'
    await query.edit_message_text("🔎 Введите название товара (или его часть):", reply_markup=kb_back_to_menu())

async def search_name_result(message, text, context):
    if not text.strip():
        await message.reply_text("❌ Введите название для поиска.", reply_markup=kb_back_to_menu())
        return True
    search_term = f"%{text.strip().lower()}%"
    async for session in get_session():
        products = (await session.execute(
            select(Product).where(Product.is_active == True, func.lower(Product.name).like(search_term))
        )).scalars().all()

    if not products:
        await message.reply_text("🔎 Товары не найдены. Введите другой запрос или нажмите «Назад».", reply_markup=kb_back_to_menu())
        return True

    new_msgs = []
    for product in products:
        name = escape_markdown(product.name)
        article = escape_markdown(product.article) if product.article else None
        msg_text = name
        if article:
            msg_text += f"\nАртикул {article}"
        if product.stock is not None:
            msg_text += f"\nНа складе: {product.stock}"
        msg_text += f"\n\nЦена {product.price:.0f} ₽"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Заказать", callback_data=f"order:start:{product.id}")]])
        try:
            if product.photo_file_ids:
                photo = product.photo_file_ids.split(',')[0]
                await message.reply_photo(photo=photo, caption=msg_text, reply_markup=kb, parse_mode="Markdown")
            else:
                await message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")
    context.user_data['search_results'] = new_msgs
    return True


# ================== ПОИСК ПО АРТИКУЛУ ==================
async def search_article_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'search_article'
    await query.edit_message_text("🔎 Введите артикул:", reply_markup=kb_back_to_menu())

async def search_article_result(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('state') != 'search_article':
        return
    message = update.message
    article_input = message.text.strip()
    if not article_input:
        await message.reply_text("❌ Введите артикул.", reply_markup=kb_back_to_menu())
        return
    async for session in get_session():
        product = (await session.execute(
            select(Product).where(Product.is_active == True, Product.article == article_input)
        )).scalar_one_or_none()
        break
    if not product:
        await message.reply_text("🔎 Товар с таким артикулом не найден.", reply_markup=kb_back_to_menu())
        context.user_data.pop('state', None)
        return

    name = escape_markdown(product.name)
    article = escape_markdown(product.article) if product.article else None
    text = name
    if article:
        text += f"\nАртикул {article}"
    if product.stock is not None:
        text += f"\nНа складе: {product.stock}"
    text += f"\n\nЦена {product.price:.0f} ₽"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Заказать", callback_data=f"order:start:{product.id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])
    try:
        if product.photo_file_ids:
            photo = product.photo_file_ids.split(',')[0]
            await message.reply_photo(photo=photo, caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Ошибка отправки фото для артикула {article_input}: {e}")
        await message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop('state', None)


# ================== РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ==================
def register(app):
    # Категории
    app.add_handler(CallbackQueryHandler(catalog_show_categories, pattern='^catalog:show$'))
    app.add_handler(CallbackQueryHandler(
        lambda update, context: catalog_show_categories(update, context,
                                                        page=int(update.callback_query.data.split(":")[-1])),
        pattern='^catalog:catlist:'
    ))
    app.add_handler(CallbackQueryHandler(
        lambda update, context: catalog_show_subcategories(update, context,
                                                           page=int(update.callback_query.data.split(":")[-1])),
        pattern='^catalog:sublist:'
    ))
    # Выбор категории → сохраняем и показываем подкатегории
    async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split(":")[2])
        cats = context.user_data.get('catalog_cats', {})
        category = cats.get(idx)
        if not category:
            await query.answer("Категория не найдена.", show_alert=True)
            return
        context.user_data['catalog_current_cat'] = category
        context.user_data.pop('catalog_current_sub', None)
        await catalog_show_subcategories(update, context, page=0)

    app.add_handler(CallbackQueryHandler(select_category, pattern='^catalog:sc:'))

    # Выбор подкатегории → сохраняем и показываем товары
    async def select_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split(":")[2])
        subs = context.user_data.get('catalog_subs', {})
        subcategory = subs.get(idx)
        if not subcategory:
            await query.answer("Подкатегория не найдена.", show_alert=True)
            return
        context.user_data['catalog_current_sub'] = subcategory
        await show_products_page(query, context, page=0)

    app.add_handler(CallbackQueryHandler(select_subcategory, pattern='^catalog:ss:'))

    # Кнопка "Назад к подкатегориям" из товаров
    async def back_to_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        context.user_data.pop('catalog_current_sub', None)
        await catalog_show_subcategories(update, context, page=0)

    app.add_handler(CallbackQueryHandler(back_to_subs, pattern='^catalog:back_to_subs$'))

    # Пагинация товаров
    async def prodpage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        page = int(query.data.split(":")[2])
        await show_products_page(query, context, page=page)

    app.add_handler(CallbackQueryHandler(prodpage_handler, pattern='^catalog:prodpage:'))

    # Заказ
    app.add_handler(CallbackQueryHandler(start_order, pattern='^order:start:'))

    # Поиск
    app.add_handler(CallbackQueryHandler(search_name_start, pattern='^search:name$'))
    app.add_handler(CallbackQueryHandler(search_article_start, pattern='^search:article$'))