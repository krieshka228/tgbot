import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from sqlalchemy import select, func
from bot.db import get_session, Product, get_or_create_user, get_or_create_draft, add_item_to_order, Order, OrderItem, get_bot_setting
from sqlalchemy.orm import selectinload
from bot.keyboards import kb_cart_actions, kb_back_to_menu
from bot.utils import format_cart, parse_quantity
from bot.filters import StateFilter
from telegram import InputMediaPhoto, InputMediaVideo
from bot.utils import escape_markdown
from bot.config import ADMIN_USER_ID


logger = logging.getLogger(__name__)
ITEMS_PER_PAGE = 3

_catalog_messages: dict[int, list[str]] = {}


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

    photos = product.photo_file_ids.split(',') if product.photo_file_ids else []
    videos = product.video_file_ids.split(',') if product.video_file_ids else []

    try:
        if len(photos) == 1 and not videos:
            await message.reply_photo(photo=photos[0], caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        elif len(videos) == 1 and not photos:
            await message.reply_video(video=videos[0], caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
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

            msgs = await message.reply_media_group(media=media)
            await message.reply_text("Выберите действие:", reply_markup=kb)
        else:
            await message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Ошибка при отправке медиа для артикула {article_input}: {e}")
        await message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop('state', None)

async def delete_catalog_messages(user_id: int, bot, also_delete_message_id: str | None = None):
    """Удаляет все сохранённые сообщения каталога для пользователя."""
    ids_to_delete = _catalog_messages.pop(user_id, [])[:]
    if also_delete_message_id and also_delete_message_id not in ids_to_delete:
        ids_to_delete.append(also_delete_message_id)
    for mid in ids_to_delete:
        try:
            await bot.delete_message(mid)
        except Exception:
            pass

async def search_article_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['state'] = 'search_article'
    await query.edit_message_text("🔎 Введите артикул:", reply_markup=kb_back_to_menu())

async def show_category_page(query, context, category: str, page: int):
    async for session in get_session():
        total = (await session.execute(
            select(func.count(Product.id)).where(Product.is_active == True, Product.category == category)
        )).scalar()
        products = (await session.execute(
            select(Product).where(Product.is_active == True, Product.category == category)
            .order_by(Product.id).offset(page * ITEMS_PER_PAGE).limit(ITEMS_PER_PAGE)
        )).scalars().all()
    if not products:
        await query.edit_message_text(f"В категории «{category}» пока нет товаров.",
                                       reply_markup=kb_back_to_menu())
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

        # Пытаемся отправить фото/видео с прикреплённой кнопкой
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
                # Кнопку добавляем отдельным сообщением, но уже без дублирования текста
                btn_msg = await bot.send_message(chat_id=chat_id, text="Выберите действие:", reply_markup=kb)
                new_msgs.append(btn_msg.message_id)
            else:
                msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
                new_msgs.append(msg.message_id)
        except Exception as e:
            logger.warning(f"Ошибка отправки медиа для товара {product.id}: {e}")
            # При ошибке отправляем просто текст с кнопкой
            msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            new_msgs.append(msg.message_id)

    # Навигационное сообщение
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"catalog:catpage:{category}:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Вперёд →", callback_data=f"catalog:catpage:{category}:{page+1}"))
    kb_nav = []
    if nav:
        kb_nav.append(nav)
    kb_nav.append([InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")])
    kb_nav.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    nav_text = f"**{category}** (стр. {page+1}/{total_pages})"
    msg_nav = await bot.send_message(
        chat_id=chat_id,
        text=nav_text,
        reply_markup=InlineKeyboardMarkup(kb_nav),
        parse_mode=ParseMode.MARKDOWN
    )
    new_msgs.append(msg_nav.message_id)

    context.user_data['catalog_messages'] = new_msgs

    # Удаляем временное сообщение "Загрузка товаров..."
    try:
        await query.message.delete()
    except Exception:
        pass

async def catalog_show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    """Показывает категории с пагинацией, отправляя новое сообщение."""
    query = update.callback_query
    await query.answer()

    # Удаляем ВСЕ предыдущие сообщения каталога, включая то, что вызвало этот callback
    for msg_id in context.user_data.pop('catalog_messages', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass
    # Удаляем и само сообщение с кнопкой «↩️ К категориям»
    try:
        await query.message.delete()
    except Exception:
        pass

    async for session in get_session():
        all_categories = (await session.execute(
            select(Product.category)
            .where(Product.is_active == True, Product.category != None)
            .distinct()
            .order_by(Product.category)
        )).scalars().all()

    if not all_categories:
        msg = await query.message.reply_text("📭 В каталоге пока нет категорий.", reply_markup=kb_back_to_menu())
        context.user_data['catalog_messages'] = [msg.message_id]
        return

    total = len(all_categories)
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_categories = all_categories[start:end]

    kb = []
    for cat in page_categories:
        kb.append([InlineKeyboardButton(cat, callback_data=f"catalog:category:{cat}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"catalog:catlist:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"catalog:catlist:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])

    nav_text = f"**Категории** (стр. {page+1}/{total_pages})"
    # Отправляем новое сообщение вместо попытки редактировать удалённое
    msg = await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=nav_text,
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN
    )
    context.user_data['catalog_messages'] = [msg.message_id]


async def catalog_category_first_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.split(":", 2)[2]

    # Оставляем сообщение с категориями, просто меняем текст на индикатор загрузки
    try:
        await query.edit_message_text("⏳ Загрузка товаров...")
    except Exception:
        pass

    # Удаляем только старые карточки товаров, если они есть
    for msg_id in context.user_data.pop('catalog_messages', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass

    await show_category_page(query, context, category, 0)

async def catalog_category_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    category = parts[2]
    page = int(parts[3])

    # Индикатор загрузки (редактируем текущее навигационное сообщение)
    try:
        await query.edit_message_text("⏳ Загрузка товаров...")
    except Exception:
        pass

    # Удаляем старые карточки товаров
    for msg_id in context.user_data.pop('catalog_messages', []):
        try:
            await context.bot.delete_message(chat_id=query.message.chat_id, message_id=msg_id)
        except Exception:
            pass

    await show_category_page(query, context, category, page)

async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_USER_ID:
        async for session in get_session():
            token = await get_bot_setting(session, "payment_qr_token")
            if not token:
                await query.answer("⚠️ Бот временно недоступен.", show_alert=True)
                return
            break
    product_id = int(query.data.split(":")[-1])
    async for session in get_session():
        product = await session.get(Product, product_id)
        break
    if not product or not product.is_active:
        await query.answer("Товар недоступен.", show_alert=True)
        return
    await query.message.delete()

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
            msg = await query.message.reply_photo(photo=photos[0], caption=text, reply_markup=kb_back_to_menu(), parse_mode=ParseMode.MARKDOWN)
            card_msg_id = msg.message_id
        elif len(videos) == 1 and not photos:
            msg = await query.message.reply_video(video=videos[0], caption=text, reply_markup=kb_back_to_menu(), parse_mode=ParseMode.MARKDOWN)
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
        logger.warning(f"Ошибка при отправке медиа товара #{product.id}: {e}")
        msg = await query.message.reply_text(text, reply_markup=kb_back_to_menu(), parse_mode=ParseMode.MARKDOWN)
        card_msg_id = msg.message_id

    context.user_data['state'] = 'order_qty'
    context.user_data['data'] = {'product_id': product_id, 'card_msg_id': card_msg_id}

def register(app):
    app.add_handler(CallbackQueryHandler(catalog_show_categories, pattern='^catalog:show$'))
    app.add_handler(CallbackQueryHandler(
        lambda update, context: catalog_show_categories(update, context,
                                                        page=int(update.callback_query.data.split(":")[-1])),
        pattern='^catalog:catlist:'
    ))
    app.add_handler(CallbackQueryHandler(catalog_category_first_page, pattern='^catalog:category:'))
    app.add_handler(CallbackQueryHandler(catalog_category_page, pattern='^catalog:catpage:'))
    app.add_handler(CallbackQueryHandler(start_order, pattern='^order:start:'))
    app.add_handler(CallbackQueryHandler(search_article_start, pattern='^search:article$'))
    # Обработчики текстовых сообщений для состояний перенесены в fsm_inputs.py