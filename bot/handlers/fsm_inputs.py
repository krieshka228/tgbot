import logging
import uuid
import asyncio
import types
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.constants import ParseMode
from bot.db import get_session, get_or_create_user, get_order_with_items, OrderStatus, Product, Order, OrderItem, PendingOrder, get_bot_setting, set_bot_setting
from bot.db import get_all_active_products, invalidate_catalog_cache
from bot.keyboards import kb_main_menu, kb_back_to_menu, kb_cart_actions, kb_admin_menu, reply_main_menu
from bot.config import ADMIN_USER_ID, ADMIN_CHAT_ID, DISCUSSION_GROUP_ID
from bot.utils import parse_quantity, _parse_post_link, format_cart, parse_post_product, escape_markdown
from bot.validators import normalize_phone, parse_positive_int, parse_non_negative_int
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from bot.db import upsert_product
from sqlalchemy import delete as sql_delete
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, MessageOriginChannel
from bot.utils import parse_quantity, _parse_post_link, format_cart, parse_post_product, escape_markdown, upload_photo_to_max, upload_video_to_max

MEDIA_GROUP_TIMEOUT = 5
ITEMS_PER_PAGE = 3
logger = logging.getLogger(__name__)

# ================== Обработчики состояний ==================
def _resolve_post_id(message) -> str | None:
    """Определяет id поста КАНАЛА (Product.post_id) по комментарию в группе.

    В связанной группе обсуждения Telegram автоматически репостит пост канала,
    и комментарий приходит как reply на ЭТОТ репост. У репоста собственный
    ``message_id`` (id сообщения в группе), который НЕ совпадает с id поста в
    канале. Правильный id канала лежит в ``forward_origin`` репоста
    (``MessageOriginChannel.message_id``) — именно его мы сохраняли как
    ``Product.post_id``. Это и была причина, по которой товар не находился.
    """
    reply = message.reply_to_message
    if reply is not None:
        origin = getattr(reply, "forward_origin", None)
        if isinstance(origin, MessageOriginChannel):
            # Явно приводим к строке
            return str(origin.message_id)
        # Фолбэк: если это не авто-репост канала — берём id самого сообщения.
        # Но это может быть неправильно, логируем предупреждение.
        logger.debug(
            "reply_to_message without MessageOriginChannel, using reply.message_id",
            extra={"reply_id": reply.message_id}
        )
        return str(reply.message_id)
    # Темы форума: тред привязан к корневому сообщению.
    if message.message_thread_id:
        logger.debug("using message_thread_id as post_id", extra={"thread_id": message.message_thread_id})
        return str(message.message_thread_id)
    return None

async def _process_delayed_media_group(context: ContextTypes.DEFAULT_TYPE, group_id: str):
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    buffer = context.user_data.get('media_buffer')
    if not buffer or group_id not in buffer:
        logger.warning(f"Буфер не найден для группы {group_id}")
        return
    entry = buffer.pop(group_id)
    caption = entry.get('caption', '')
    photos = entry['photos']
    videos = entry['videos']
    msg_ids = entry['messages']
    chat_id = entry['chat_id']
    user_id = entry['user_id']

    logger.info(f"Обработка альбома {group_id}: фото={len(photos)}, видео={len(videos)}, caption='{caption[:50]}'")
    fake_msg = types.SimpleNamespace()
    fake_msg.from_user = types.SimpleNamespace(id=user_id)
    fake_msg.chat_id = chat_id
    fake_msg.photo = None
    fake_msg.video = None
    fake_msg.forward_origin = None
    fake_msg.message_id = msg_ids[0] if msg_ids else 0

    if caption.strip():
        await process_admin_sync(fake_msg, caption, context, photos=photos, videos=videos)
        # Удаляем исходные сообщения альбома после успешного создания товара
        for mid in msg_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception as e:
                logger.warning(f"Не удалось удалить сообщение {mid}: {e}")
    else:
        context.user_data['pending_photos'] = photos
        context.user_data['pending_videos'] = videos
        context.user_data['pending_msg_ids'] = msg_ids
        context.user_data['pending_chat_id'] = chat_id
        context.user_data['state'] = 'admin_sync_text'
        await context.bot.send_message(
            chat_id=chat_id,
            text="📝 Отправьте текст поста (название, артикул, цену…) для этого альбома.",
            reply_markup=kb_back_to_menu()
        )

async def process_order_qty(message, text, context):
    qty = parse_quantity(text)
    data = context.user_data.get('data', {})
    product_id = data.get('product_id')
    card_msg_id = data.get('card_msg_id')
    user_id = message.from_user.id

    if not qty:
        await message.reply_text("❌ Введите целое положительное число.", reply_markup=kb_back_to_menu())
        return True

    async for session in get_session():
        user = await get_or_create_user(session, user_id,
                                        full_name=message.from_user.full_name,
                                        username=message.from_user.username)
        product = await session.get(Product, product_id)
        if not product or not product.is_active:
            await message.reply_text("❌ Товар недоступен.", reply_markup=kb_back_to_menu())
            context.user_data.pop('state', None)
            return True
        if product.stock is not None and qty > product.stock:
            await message.reply_text(f"❌ Недостаточно товара. В наличии: {product.stock} шт.", reply_markup=kb_back_to_menu())
            context.user_data.pop('state', None)
            return True

        from bot.db import get_or_create_draft, add_item_to_order
        order = await get_or_create_draft(session, user_id)
        stmt = select(Order).where(Order.id == order.id).options(selectinload(Order.items).selectinload(OrderItem.product))
        order = (await session.execute(stmt)).scalar_one()
        await add_item_to_order(session, order, product, qty)
        # Обновляем order
        order = (await session.execute(stmt)).scalar_one()

    # --- НОВОЕ СООБЩЕНИЕ С ПОДТВЕРЖДЕНИЕМ ---
    safe_name = escape_markdown(product.name)
    confirm_text = f"✅ **{safe_name}** × {qty} шт. добавлен в корзину!"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Перейти в корзину", callback_data="cart:view")],
        [InlineKeyboardButton("📦 Продолжить покупки", callback_data="catalog:show")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])

    # Если карточка товара была отправлена – редактируем её
    if card_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=card_msg_id,
                text=confirm_text,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            # Если редактирование не удалось, отправляем новое сообщение
            await message.reply_text(confirm_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await message.reply_text(confirm_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop('state', None)
    context.user_data.pop('data', None)
    return True

async def process_cart_change_qty(message, text, context):
    data = context.user_data.get('data', {})
    item_id = data.get('item_id')
    if not item_id:
        await message.reply_text("❌ Ошибка. Попробуйте снова.")
        context.user_data.pop('state', None)
        return True
    new_qty = parse_positive_int(text)
    if new_qty is None:
        await message.reply_text("❌ Введите целое положительное число.", reply_markup=kb_back_to_menu())
        return True
    user_id = message.from_user.id
    async for session in get_session():
        from bot.db import get_draft_order, recalculate_total
        order = await get_draft_order(session, user_id)
        if not order:
            await message.reply_text("❌ Корзина не найдена.", reply_markup=kb_back_to_menu())
            context.user_data.pop('state', None)
            return True
        item = next((i for i in order.items if i.id == item_id), None)
        if not item:
            await message.reply_text("❌ Позиция не найдена.", reply_markup=kb_back_to_menu())
            context.user_data.pop('state', None)
            return True
        product = item.product
        if product and product.stock is not None and new_qty > product.stock:
            await message.reply_text(f"❌ Недостаточно товара. В наличии: {product.stock} шт.", reply_markup=kb_back_to_menu())
            context.user_data.pop('state', None)
            return True
        item.quantity = new_qty
        await recalculate_total(session, order)
        await session.commit()
    async for session in get_session():
        order = await get_draft_order(session, user_id)
    await message.reply_text(format_cart(order),
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_cart_actions(order.id))
    context.user_data.pop('state', None)
    return True

async def process_search_article(message, text, context):
    article = text.strip()
    if not article:
        await message.reply_text("❌ Введите артикул.", reply_markup=kb_back_to_menu())
        return True

    async for session in get_session():
        products = (await session.execute(
            select(Product).where(Product.article == article, Product.is_active == True)
        )).scalars().all()
        break

    if not products:
        await message.reply_text("🔎 Товар с таким артикулом не найден.", reply_markup=kb_back_to_menu())
        context.user_data.pop('state', None)
        return True

    # Берём первый активный товар
    product = products[0]

    msg_text = escape_markdown(product.name)
    if product.article:
        msg_text += f"\nАртикул {escape_markdown(product.article)}"
    if product.stock is not None:
        msg_text += f"\nНа складе: {product.stock}"
    msg_text += f"\n\nЦена {product.price:.0f} ₽"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Заказать", callback_data=f"order:start:{product.id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])

    try:
        if product.photo_file_ids:
            photo = product.photo_file_ids.split(',')[0]
            await message.reply_photo(photo=photo, caption=msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.reply_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.warning(f"Ошибка отправки фото для артикула {article}: {e}")
        await message.reply_text(msg_text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop('state', None)
    return True

async def process_search_name(message, text, context):
    if not text.strip():
        await message.reply_text("❌ Введите название для поиска.", reply_markup=kb_back_to_menu())
        return True

    search_substr = text.strip().lower()
    async for session in get_session():
        all_products = await get_all_active_products(session)
        matched = [p for p in all_products if search_substr in p.name.lower()]

    if not matched:
        await message.reply_text("🔎 Товары не найдены. Введите другой запрос или нажмите «Назад».", reply_markup=kb_back_to_menu())
        return True

    context.user_data['search_results'] = matched
    context.user_data['search_nav_msg_id'] = None
    await show_search_results_page(message, context, matched, page=0)
    return True

async def show_search_results_page(message, context, products: list, page: int):
    total = len(products)
    total_pages = (total - 1) // ITEMS_PER_PAGE + 1
    start = page * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = products[start:end]

    lines = [f"🔎 **Результаты поиска** (стр. {page+1}/{total_pages})\n"]
    kb = []
    for idx, p in enumerate(page_items, start=start):
        name = escape_markdown(p.name)
        article = f" (арт. {p.article})" if p.article else ""
        button_text = f"{name}{article}"
        kb.append([InlineKeyboardButton(button_text, callback_data=f"search:select:{idx}")])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("← Назад", callback_data=f"search:page:{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Вперёд →", callback_data=f"search:page:{page+1}"))
    if nav_buttons:
        kb.append(nav_buttons)
    kb.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])

    prev_msg_id = context.user_data.get('search_nav_msg_id')
    if prev_msg_id:
        try:
            await context.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=prev_msg_id,
                text="\n".join(lines),
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            msg = await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            context.user_data['search_nav_msg_id'] = msg.message_id
    else:
        msg = await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        context.user_data['search_nav_msg_id'] = msg.message_id

async def process_awaiting_phone(message, text, context):
    phone = normalize_phone(text)
    if not phone:
        await message.reply_text("❌ Введите корректный номер телефона (10–15 цифр, можно с +).")
        return True
    user_id = message.from_user.id
    order_id = None
    data = context.user_data.get('data', {})
    if data.get('order_id'):
        order_id = data['order_id']
    else:
        async for session in get_session():
            stmt = select(Order).where(
                Order.user_id == user_id,
                Order.status == OrderStatus.confirmed,
                Order.contact_phone == None
            ).order_by(Order.updated_at.desc()).limit(1)
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()
            if order:
                order_id = order.id
    if not order_id:
        await message.reply_text("❌ Нет подтверждённого заказа для ввода телефона.")
        context.user_data.pop('state', None)
        return True
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await message.reply_text("❌ Заказ не найден.")
            context.user_data.pop('state', None)
            return True
        order.contact_phone = phone
        user = await get_or_create_user(session, user_id)
        user.phone = phone
        await session.commit()
    # Переход к запросу ФИО вместо сразу доставки
    context.user_data['state'] = 'awaiting_full_name'
    context.user_data['data'] = {'order_id': order_id}
    await message.reply_text("✏️ Введите ваше полное имя (ФИО) для заказа:")
    return True
async def process_awaiting_full_name(message, text, context):
    full_name = text.strip()
    if not full_name:
        await message.reply_text("❌ Введите ваше полное имя (ФИО).")
        return True
    user_id = message.from_user.id
    data = context.user_data.get('data', {})
    order_id = data.get('order_id')
    if not order_id:
        await message.reply_text("❌ Нет активного заказа для заполнения ФИО.")
        context.user_data.pop('state', None)
        return True
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await message.reply_text("❌ Заказ не найден.")
            context.user_data.pop('state', None)
            return True
        order.full_name = full_name
        # Сохраняем в профиль пользователя
        user = await get_or_create_user(session, user_id)
        user.full_name = full_name
        await session.commit()
    # Переход к выбору доставки
    context.user_data['state'] = 'awaiting_delivery_method'
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Озон", callback_data="delivery:ozon"),
         InlineKeyboardButton("Яндекс", callback_data="delivery:yandex")],
        [InlineKeyboardButton("СДЭК до ПВЗ", callback_data="delivery:cdek")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])
    await message.reply_text("✅ ФИО сохранено!\n\n🚚 Теперь выберите способ доставки:", reply_markup=kb)
    return True
async def process_awaiting_address(message, text, context):
    address = text.strip()
    if not address:
        await message.reply_text("Пожалуйста, напишите адрес доставки текстом.")
        return True
    data = context.user_data.get('data', {})
    order_id = data.get('order_id')
    user_id = message.from_user.id
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await message.reply_text("❌ Заказ не найден.")
            context.user_data.pop('state', None)
            return True
        order.delivery_address = address
        user = await get_or_create_user(session, user_id)
        user.address = address
        await session.commit()
        phone = user.phone or "не указан"
        method = order.delivery_method or "не выбран"
        total = order.total_amount
    context.user_data['state'] = 'confirm_customer_data'
    summary = (
        f"📋 **Проверьте данные заказа:**\n\n"
        f"📱 Телефон: {phone}\n"
        f"🚚 Доставка: {method}\n"
        f"📍 Адрес: {escape_markdown(address)}\n"
        f"💰 Сумма: {total:.0f} ₽"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_data:yes")],
        [InlineKeyboardButton("✏️ Изменить адрес", callback_data="confirm_data:edit_address")],
        [InlineKeyboardButton("📱 Изменить телефон", callback_data="confirm_data:edit_phone")],
    ])
    await message.reply_text(summary, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    return True

async def confirm_data_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if context.user_data.get('state') != 'confirm_customer_data':
        return
    data = context.user_data.get('data', {})
    order_id = data.get('order_id')
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if not order or order.user_id != user_id:
            await query.edit_message_text("❌ Заказ не найден.")
            context.user_data.pop('state', None)
            return
        user = order.user
        # ФИО для отправки: берём из заказа, иначе из профиля пользователя
        # (Telegram-имя). Если ничего нет — оставляем поле пустым.
        if not order.full_name and user and user.full_name:
            order.full_name = user.full_name
            await session.commit()
        fio = order.full_name or (user.full_name if user else None)
        full_name = escape_markdown(fio) if fio else 'Без имени'
        address = escape_markdown(order.delivery_address) if order.delivery_address else 'не указан'
        admin_text = f"📦 **Заказ #{order_id} готов к отправке**\n\n" \
                     f"👤 Клиент: {full_name} (ID {user.id})\n" \
                     f"📱 Телефон: {user.phone or 'не указан'}\n" \
                     f"🚚 Доставка: {order.delivery_method}\n" \
                     f"📍 Адрес: {address}\n\n" \
                     f"💰 Итого: {order.total_amount:.0f} ₽"
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode=ParseMode.MARKDOWN)
            logger.info("order ready, admin notified",
                        extra={"event": "order_ready", "user_id": user_id, "order_id": order_id})
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление: {e}")
    context.user_data.pop('state', None)
    await query.edit_message_text(f"✅ Данные сохранены!\n\nЗаказ #{order_id} принят в работу!",
                                  reply_markup=kb_main_menu(is_admin=(user_id == ADMIN_USER_ID)))

async def confirm_data_edit_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if context.user_data.get('state') != 'confirm_customer_data':
        return
    context.user_data['state'] = 'awaiting_address'
    await query.edit_message_text("📍 Введите новый адрес доставки:", reply_markup=kb_back_to_menu())

async def confirm_data_edit_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if context.user_data.get('state') != 'confirm_customer_data':
        return
    context.user_data['state'] = 'awaiting_phone'
    await query.edit_message_text("📱 Введите новый номер телефона:", reply_markup=kb_back_to_menu())

async def process_contact_admin(message, text, context):
    if not text.strip():
        await message.reply_text("Напишите ваш вопрос текстом.")
        return True
    sender_name = escape_markdown(message.from_user.full_name) if message.from_user.full_name else f"ID {message.from_user.id}"
    fwd_text = f"✉️ **Сообщение от клиента**\nКлиент: {sender_name} (ID: {message.from_user.id})\n\n{escape_markdown(text.strip())}"
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=fwd_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Не удалось переслать сообщение: {e}")
        await message.reply_text("❌ Не удалось отправить сообщение администратору. Попробуйте позже.")
        context.user_data.pop('state', None)
        return True
    context.user_data.pop('state', None)
    await message.reply_text("✅ Сообщение отправлено администратору!",
                             reply_markup=kb_main_menu(is_admin=(message.from_user.id == ADMIN_USER_ID)))
    return True

async def process_admin_sync(message, text, context, photos=None, videos=None):
    if message.from_user.id != ADMIN_USER_ID:
        return True
    if not text or not text.strip():
        if message.photo or message.video:
            await context.bot.send_message(
                chat_id=message.chat_id,
                text="❌ Для синхронизации нужен текст. Перешлите пост с подписью или скопируйте текст."
            )
        else:
            await context.bot.send_message(
                chat_id=message.chat_id,
                text="❌ Не удалось получить текст из сообщения."
            )
        return True

    name, article, price, category, description, stock = parse_post_product(text)
    if not name or not article:
        await context.bot.send_message(
            chat_id=message.chat_id,
            text="❌ Пост должен содержать название и артикул."
        )
        return True

    async for session in get_session():
        stmt = select(Product).where(Product.name == name)
        if article:
            stmt = stmt.where(Product.article == article)
        existing = (await session.execute(stmt)).scalars().all()
        if existing:
            try:
                await message.delete()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=message.chat_id,
                text=f"⚠️ Товар «{name}» уже существует (арт. {article or '—'}). Пропущен."
            )
            context.user_data['sync_skipped'] = context.user_data.get('sync_skipped', 0) + 1
            return True

    if message.forward_origin and hasattr(message.forward_origin, 'message_id'):
        post_id = str(message.forward_origin.message_id)
    else:
        post_id = f"manual_{uuid.uuid4().hex[:8]}"

    if photos is None:
        photos = [message.photo[-1].file_id] if message.photo else []
    if videos is None:
        videos = [message.video.file_id] if message.video else []
    photo_file_ids = ",".join(photos) if photos else None
    video_file_ids = ",".join(videos) if videos else None

    # --- Загрузка медиа в Max (ключевое исправление!) ---
    max_photo_ids = None
    if photos:
        tokens = []
        for file_id in photos:
            token = await upload_photo_to_max(file_id, context.bot)
            if token:
                tokens.append(token)
        max_photo_ids = ",".join(tokens) if tokens else None

    max_video_ids = None
    if videos:
        tokens = []
        for file_id in videos:
            token = await upload_video_to_max(file_id, context.bot)
            if token:
                tokens.append(token)
        max_video_ids = ",".join(tokens) if tokens else None

    async for session in get_session():
        product = await upsert_product(
            session, post_id, name, price,
            photo_file_ids=photo_file_ids,
            video_file_ids=video_file_ids,
            max_photo_ids=max_photo_ids,
            max_video_ids=max_video_ids,
            article=article, category=category,
            description=description,
            in_stock=(stock is not None and stock > 0),
            stock=stock,
        )

    try:
        await message.delete()
    except Exception:
        pass

    last_msg_id = context.user_data.get('last_sync_msg_id')
    if last_msg_id:
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=last_msg_id)
        except Exception:
            pass

    if product.is_active:
        status_note = f"виден покупателям, остаток {product.stock} шт."
    else:
        status_note = f"скрыт, остаток {product.stock or 0} шт."
    await context.bot.send_message(
        chat_id=message.chat_id,
        text=f"✅ Добавлен товар: «{name}» ({status_note})",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]])
    )
    context.user_data['sync_count'] = context.user_data.get('sync_count', 0) + 1
    return True

async def process_admin_delete_by_articles(message, text, context):
    if message.from_user.id != ADMIN_USER_ID:
        return True
    articles_input = text.strip()
    if not articles_input:
        await message.reply_text("❌ Введите хотя бы один артикул.", reply_markup=kb_back_to_menu())
        return True

    # Разбиваем строку по запятым, пробелам, переводам строк
    import re
    parts = re.split(r'[,\s\n]+', articles_input)
    articles = [p for p in parts if p]  # убираем пустые строки

    deleted_names = []
    not_found = []

    async for session in get_session():
        for art in articles:
            # Ищем ВСЕ товары с таким артикулом
            products = (await session.execute(
                select(Product).where(Product.article == art)
            )).scalars().all()
            if products:
                for product in products:
                    # Удаляем связанные позиции заказов
                    await session.execute(
                        sql_delete(OrderItem).where(OrderItem.product_id == product.id)
                    )
                    deleted_names.append(product.name)
                    await session.delete(product)
            else:
                not_found.append(art)
        await session.commit()

    if deleted_names:
        invalidate_catalog_cache()

    # Формируем ответ
    text_parts = []
    if deleted_names:
        text_parts.append(f"✅ Удалено товаров: {len(deleted_names)}")
        for name in deleted_names[:10]:  # покажем не более 10 имён
            text_parts.append(f"  • {name}")
        if len(deleted_names) > 10:
            text_parts.append(f"  … и ещё {len(deleted_names)-10}")
    if not_found:
        text_parts.append(f"❌ Не найдено артикулов: {', '.join(not_found)}")
    if not text_parts:
        text_parts.append("ℹ️ Ни один товар не удалён.")

    context.user_data.pop('state', None)
    await message.reply_text("\n".join(text_parts), reply_markup=kb_admin_menu())
    return True
async def process_admin_set_stock(message, text, context):
    if message.from_user.id != ADMIN_USER_ID:
        return True
    data = context.user_data.get('data', {})
    product_id = data.get('product_id')
    new_stock = parse_non_negative_int(text)
    if new_stock is None:
        await message.reply_text("❌ Введите целое неотрицательное число.", reply_markup=kb_back_to_menu())
        return True

    async for session in get_session():
        product = await session.get(Product, product_id)
        if not product:
            await message.reply_text("❌ Товар не найден.", reply_markup=kb_admin_menu())
            context.user_data.pop('state', None)
            return True
        product.stock = new_stock
        product.is_active = new_stock > 0
        product.in_stock = new_stock > 0
        await session.commit()
        invalidate_catalog_cache()

    # Удаляем сообщение пользователя с числом
    try:
        await message.delete()
    except Exception:
        pass

    # Получаем контекст возврата и ID сообщения с запросом остатка
    return_context = context.user_data.pop('admin_return_context', None)
    stock_msg_id = context.user_data.pop('admin_stock_msg_id', None)
    context.user_data.pop('state', None)

    if return_context and stock_msg_id:
        cat = return_context.get('category')
        sub = return_context.get('subcategory')
        page = return_context.get('page', 0)
        if cat:
            # Создаём фейковый callback query, чтобы переиспользовать show_stock_products_page
            class FakeQuery:
                def __init__(self, chat_id, message_id):
                    self.message = type('obj', (object,), {
                        'chat_id': chat_id,
                        'message_id': message_id,
                        'reply_text': None
                    })()
                async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
                    return await context.bot.edit_message_text(
                        chat_id=self.message.chat_id,
                        message_id=self.message.message_id,
                        text=text,
                        reply_markup=reply_markup,
                        parse_mode=parse_mode
                    )
            fake_query = FakeQuery(message.chat_id, stock_msg_id)
            from bot.handlers.admin import show_stock_products_page
            await show_stock_products_page(fake_query, context, cat, page, subcategory=sub)
            return True

    # Если контекст не сохранился – просто подтверждение
    await message.reply_text("✅ Остаток обновлён.", reply_markup=kb_admin_menu())
    return True

async def process_admin_search_article(message, text, context):
    article = text.strip()
    async for session in get_session():
        product = (await session.execute(select(Product).where(Product.article == article))).scalar_one_or_none()
    if not product:
        await message.reply_text("❌ Товар с таким артикулом не найден.", reply_markup=kb_admin_menu())
        context.user_data.pop('state', None)
        return True
    context.user_data['state'] = 'admin_set_stock'
    context.user_data['data'] = {'product_id': product.id}
    await message.reply_text(
        f"Товар: {product.name}\nТекущий остаток: {product.stock}\n✏️ Введите новый остаток:",
        reply_markup=kb_back_to_menu()
    )
    return True

async def process_admin_link_post(message, text, context):
    """Ручная привязка товара к посту: ввод '<product_id> <post_id>'."""
    if message.from_user.id != ADMIN_USER_ID:
        return True
    parts = text.split()
    if len(parts) != 2 or not parts[0].isdigit():
        await message.reply_text(
            "❌ Формат: <код товара> <post_id>. Например: 42 1287",
            reply_markup=kb_back_to_menu(),
        )
        return True
    product_id = int(parts[0])
    post_id = parts[1].strip()

    async for session in get_session():
        product = await session.get(Product, product_id)
        if not product:
            await message.reply_text(f"❌ Товар #{product_id} не найден.",
                                     reply_markup=kb_admin_menu())
            context.user_data.pop('state', None)
            return True
        product.post_id = post_id
        await session.commit()
        invalidate_catalog_cache()
        product_name = product.name

    context.user_data.pop('state', None)
    logger.info("admin linked post to product",
                extra={"event": "link_post", "product_id": product_id, "post_id": post_id})
    await message.reply_text(
        f"✅ Товар «{product_name}» (#{product_id}) привязан к посту {post_id}.",
        reply_markup=kb_admin_menu(),
    )
    return True


async def process_direct_order(message, text, context):
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2:
        return False
    post_id = _parse_post_link(parts[0])
    qty = parse_quantity(parts[1])
    if post_id is None or qty is None:
        return False
    user_id = message.from_user.id
    async for session in get_session():
        user = await get_or_create_user(session, user_id)
        stmt = select(Product).where(Product.post_id == post_id, Product.is_active == True)
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()
        if not product:
            await message.reply_text("⚠️ Товар с таким артикулом/постом не найден.")
            return True
        from bot.db import get_or_create_draft, add_item_to_order
        order = await get_or_create_draft(session, user_id)
        stmt = select(Order).where(Order.id == order.id).options(selectinload(Order.items).selectinload(OrderItem.product))
        order = (await session.execute(stmt)).scalar_one()
        await add_item_to_order(session, order, product, qty)
        order = (await session.execute(stmt)).scalar_one()
        cart_text = format_cart(order)
    await message.reply_text(f"✅ **{product.name}** × {qty} шт. добавлен в корзину!\n\n{cart_text}",
                             parse_mode=ParseMode.MARKDOWN,
                             reply_markup=kb_cart_actions(order.id))
    return True


async def porder_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение отложенного заказа из комментария."""
    logger.info("=== porder_confirm START ===")
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    product_id = int(parts[2])
    qty = int(parts[3])
    user_id = query.from_user.id
    logger.info(f"product_id={product_id}, qty={qty}, user_id={user_id}")

    # Проверяем наличие QR-кода
    async for session in get_session():
        logger.info("Checking QR token...")
        token = await get_bot_setting(session, "payment_qr_token")
        logger.info(f"QR token exists: {bool(token)}")
        if not token:
            logger.warning("QR token missing")
            if user_id == ADMIN_USER_ID:
                await query.edit_message_text("⚠️ QR-код не задан. Загрузите его в админ‑меню.")
            else:
                await query.edit_message_text("⚠️ Бот временно недоступен.")
            stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
            pending = (await session.execute(stmt)).scalar_one_or_none()
            if pending:
                logger.info(f"Deleting PendingOrder for user {user_id}")
                if pending.confirmation_msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=user_id,
                            message_id=pending.confirmation_msg_id
                        )
                    except Exception as e:
                        logger.warning(f"Failed to delete confirmation msg: {e}")
                await session.delete(pending)
                await session.commit()
            return
        break

    async for session in get_session():
        logger.info(f"Getting product {product_id}...")
        product = await session.get(Product, product_id)
        logger.info(f"Product found: {product is not None}, active: {product.is_active if product else False}")
        if not product or not product.is_active:
            logger.warning("Product not found or inactive")
            await query.edit_message_text("❌ Товар недоступен.")
            stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
            pending = (await session.execute(stmt)).scalar_one_or_none()
            if pending:
                if pending.confirmation_msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=user_id,
                            message_id=pending.confirmation_msg_id
                        )
                    except Exception:
                        pass
                await session.delete(pending)
                await session.commit()
            context.user_data.pop('pending_order', None)
            context.user_data.pop('state', None)
            return

        if product.stock is not None and qty > product.stock:
            logger.warning(f"Not enough stock: requested {qty}, available {product.stock}")
            await query.edit_message_text(
                f"❌ Недостаточно товара. Доступно только {product.stock} шт.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
                ])
            )
            stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
            pending = (await session.execute(stmt)).scalar_one_or_none()
            if pending:
                if pending.confirmation_msg_id:
                    try:
                        await context.bot.delete_message(
                            chat_id=user_id,
                            message_id=pending.confirmation_msg_id
                        )
                    except Exception:
                        pass
                await session.delete(pending)
                await session.commit()
            context.user_data.pop('pending_order', None)
            context.user_data.pop('state', None)
            return

        logger.info("Reserving stock...")
        if product.stock is not None:
            product.stock -= qty
            product.is_active = product.stock > 0
            product.in_stock = product.stock > 0

        from bot.db import get_or_create_draft, add_item_to_order
        logger.info("Creating order...")
        order = await get_or_create_draft(session, user_id)
        stmt = select(Order).where(Order.id == order.id).options(
            selectinload(Order.items).selectinload(OrderItem.product)
        )
        order = (await session.execute(stmt)).scalar_one()
        await add_item_to_order(session, order, product, qty)
        order = (await session.execute(stmt)).scalar_one()
        order.status = OrderStatus.pending
        await session.commit()
        invalidate_catalog_cache()
        cart_text = format_cart(order)
        logger.info(f"Order #{order.id} created successfully")

        logger.info("Deleting PendingOrder...")
        stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
        pending = (await session.execute(stmt)).scalar_one_or_none()
        if pending:
            if pending.confirmation_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=user_id,
                        message_id=pending.confirmation_msg_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete confirmation msg: {e}")
            await session.delete(pending)
            await session.commit()
        logger.info("PendingOrder deleted")

    qr_file_id = None
    async for session in get_session():
        qr_file_id = await get_bot_setting(session, "payment_qr_telegram")
    logger.info(f"QR file_id: {qr_file_id is not None}")

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

    if qr_file_id:
        logger.info("Sending photo with order...")
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=qr_file_id,
            caption=text,
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        logger.info("Sending text message with order...")
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

    context.user_data.pop('pending_order', None)
    context.user_data.pop('state', None)
    logger.info("=== porder_confirm END ===")


async def porder_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("=== porder_cancel START ===")
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    logger.info(f"user_id={user_id}")

    async for session in get_session():
        stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
        pending = (await session.execute(stmt)).scalar_one_or_none()
        logger.info(f"PendingOrder found: {pending is not None}")
        if pending:
            if pending.confirmation_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=user_id,
                        message_id=pending.confirmation_msg_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to delete: {e}")
            await session.delete(pending)
            await session.commit()
            logger.info("PendingOrder deleted")

    context.user_data.pop('pending_order', None)
    context.user_data.pop('state', None)
    from bot.handlers.start import get_main_menu_info
    is_admin = (user_id == ADMIN_USER_ID)
    text, kb = await get_main_menu_info(is_admin)
    await query.edit_message_text(text, reply_markup=kb)
    logger.info("=== porder_cancel END ===")

async def message_dispatcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return
    content_text = message.text or message.caption
    state = context.user_data.get('state')

    if state == 'admin_payment_qr':
        if message.photo:
            file_id = message.photo[-1].file_id
            async for session in get_session():
                # Сохраняем Telegram file_id для показа в Telegram
                await set_bot_setting(session, "payment_qr_telegram", file_id)
                # Загружаем это же фото в Max и сохраняем Max-токен
                from bot.utils import upload_photo_to_max  # используем существующую функцию
                max_token = await upload_photo_to_max(file_id, context.bot)
                if max_token:
                    await set_bot_setting(session, "payment_qr_token", max_token)
            context.user_data.pop('state', None)
            await message.reply_text("✅ QR-код сохранён для Telegram и Max.", reply_markup=kb_back_to_menu())
        else:
            await message.reply_text("❌ Пришлите изображение в формате PNG.", reply_markup=kb_back_to_menu())
        return

    if state == 'admin_sync':
        if message.media_group_id:
            group_id = message.media_group_id
            buffer = context.user_data.setdefault('media_buffer', {})
            if group_id not in buffer:
                buffer[group_id] = {
                    'caption': message.caption or '',
                    'photos': [],
                    'videos': [],
                    'messages': [message.message_id],
                    'chat_id': message.chat_id,
                    'user_id': message.from_user.id
                }
            else:
                entry = buffer[group_id]
                if message.caption:
                    entry['caption'] = message.caption
                entry['messages'].append(message.message_id)
            entry = buffer[group_id]
            if message.photo:
                entry['photos'].append(message.photo[-1].file_id)
            elif message.video:
                entry['videos'].append(message.video.file_id)
            if 'task' not in entry:
                entry['task'] = asyncio.create_task(_process_delayed_media_group(context, group_id))
            return
        else:
            await process_admin_sync(message, content_text, context)
        return

    if state == 'admin_sync_text':
        photos = context.user_data.pop('pending_photos', [])
        videos = context.user_data.pop('pending_videos', [])
        msg_ids = context.user_data.pop('pending_msg_ids', [])
        chat_id = context.user_data.pop('pending_chat_id', message.chat_id)

        fake_msg = types.SimpleNamespace()
        fake_msg.from_user = types.SimpleNamespace(id=message.from_user.id)
        fake_msg.chat_id = chat_id
        fake_msg.photo = None
        fake_msg.video = None
        fake_msg.forward_origin = None
        fake_msg.message_id = msg_ids[0] if msg_ids else message.message_id

        await process_admin_sync(fake_msg, content_text, context, photos=photos, videos=videos)
        for mid in msg_ids:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass
        return

    if state == 'confirm_pending_order':
        await message.reply_text("Пожалуйста, выберите действие: подтвердить или отменить заказ.")
        return

    if state == 'admin_delete_by_articles':
        await process_admin_delete_by_articles(message, content_text, context)
        return

    if not content_text:
        return

    if state == 'order_qty':
        await process_order_qty(message, content_text, context)
    elif state == 'cart_change_qty':
        await process_cart_change_qty(message, content_text, context)
    elif state == 'search_article':
        await process_search_article(message, content_text, context)
    elif state == 'search_name':
        await process_search_name(message, content_text, context)
    elif state == 'awaiting_full_name':
        await process_awaiting_full_name(message, content_text, context)
    elif state == 'awaiting_phone':
        await process_awaiting_phone(message, content_text, context)
    elif state == 'awaiting_address':
        await process_awaiting_address(message, content_text, context)
    elif state == 'contact_admin':
        await process_contact_admin(message, content_text, context)
    elif state == 'admin_set_stock':
        await process_admin_set_stock(message, content_text, context)
    elif state == 'admin_search_article':
        await process_admin_search_article(message, content_text, context)
    elif state == 'admin_link_post':
        await process_admin_link_post(message, content_text, context)
    else:
        async for session in get_session():
            stmt = select(Order).where(
                Order.user_id == message.from_user.id,
                Order.status == OrderStatus.confirmed,
                Order.contact_phone == None
            ).order_by(Order.updated_at.desc()).limit(1)
            result = await session.execute(stmt)
            order = result.scalar_one_or_none()
            if order:
                context.user_data['state'] = 'awaiting_phone'
                context.user_data['data'] = {'order_id': order.id}
                await process_awaiting_phone(message, content_text, context)
                return
        if content_text == "🏠 Главное меню":
            is_admin = (message.from_user.id == ADMIN_USER_ID)
            await message.reply_text(
                "🏠 Главное меню:",
                reply_markup=kb_main_menu(is_admin=is_admin)
            )
            await message.reply_text(
                "\u2060",
                reply_markup=reply_main_menu()
            )
            context.user_data.pop('state', None)
            for msg_id in context.user_data.pop('catalog_messages', []):
                try:
                    await context.bot.delete_message(chat_id=message.chat_id, message_id=msg_id)
                except Exception:
                    pass
            return
        handled = await process_direct_order(message, content_text, context)
        if not handled:
            pass

async def handle_delivery_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if context.user_data.get('state') != 'awaiting_delivery_method':
        return
    method = query.data.split(":")[1]
    delivery_names = {"ozon": "Озон", "yandex": "Яндекс", "cdek": "СДЭК до ПВЗ"}
    delivery_name = delivery_names.get(method, method)
    data = context.user_data.get('data', {})
    order_id = data.get('order_id')
    user_id = query.from_user.id
    async for session in get_session():
        order = await get_order_with_items(session, order_id)
        if order and order.user_id == user_id:
            order.delivery_method = delivery_name
            await session.commit()
    context.user_data['state'] = 'awaiting_address'
    context.user_data['data'] = {'order_id': order_id}
    await query.edit_message_text(f"✅ Выбрана доставка: {delivery_name}\n📍 Теперь введите адрес доставки:",
                                   reply_markup=kb_back_to_menu())
async def contact_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопки «Написать администратору»."""
    query = update.callback_query
    await query.answer()
    logger.info("contact_admin_start", extra={"user_id": query.from_user.id})
    context.user_data['state'] = 'contact_admin'
    await query.edit_message_text("✏️ Напишите ваш вопрос администратору (текст):", reply_markup=kb_back_to_menu())
def register(app):
    # ВАЖНО: диспетчер FSM работает ТОЛЬКО в личке (PRIVATE). Раньше фильтр был
    # `~ChatType.CHANNEL`, из-за чего этот хэндлер (группа 0, регистрируется
    # раньше posts) перехватывал сообщения из группы обсуждения канала и
    # `handle_comment` в posts.py никогда не вызывался. Ограничение приватным
    # чатом освобождает сообщения группы обсуждения для обработчика комментариев.
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO) & filters.ChatType.PRIVATE,
        message_dispatcher
    ))
    app.add_handler(CallbackQueryHandler(handle_delivery_choice, pattern='^delivery:'))
    app.add_handler(CallbackQueryHandler(confirm_data_yes, pattern='^confirm_data:yes$'))
    app.add_handler(CallbackQueryHandler(confirm_data_edit_address, pattern='^confirm_data:edit_address$'))
    app.add_handler(CallbackQueryHandler(confirm_data_edit_phone, pattern='^confirm_data:edit_phone$'))
    app.add_handler(CallbackQueryHandler(porder_confirm, pattern='^porder:confirm:'))
    app.add_handler(CallbackQueryHandler(porder_cancel, pattern='^porder:cancel$'))

    # Обработчики поиска по названию
    async def search_page_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        page = int(query.data.split(":")[2])
        products = context.user_data.get('search_results', [])
        if not products:
            await query.edit_message_text("Результаты поиска устарели. Выполните новый поиск.", reply_markup=kb_back_to_menu())
            return
        await show_search_results_page(query.message, context, products, page=page)

    async def search_select_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        idx = int(query.data.split(":")[2])
        products = context.user_data.get('search_results', [])
        if idx >= len(products):
            await query.answer("Товар не найден.", show_alert=True)
            return
        product = products[idx]
        name = escape_markdown(product.name)
        article = escape_markdown(product.article) if product.article else None
        text = name
        if article:
            text += f"\nАртикул {article}"
        if product.stock is not None:
            text += f"\nНа складе: {product.stock}"
        text += f"\n\nЦена {product.price:.0f} ₽\n\n✏️ Введите количество:"
        kb = kb_back_to_menu()
        try:
            if product.photo_file_ids:
                photo = product.photo_file_ids.split(',')[0]
                msg = await context.bot.send_photo(chat_id=query.message.chat_id, photo=photo, caption=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            else:
                msg = await context.bot.send_message(chat_id=query.message.chat_id, text=text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            context.user_data['state'] = 'order_qty'
            context.user_data['data'] = {'product_id': product.id, 'card_msg_id': msg.message_id}
        except Exception as e:
            logger.error(f"Ошибка при показе товара из поиска: {e}")
            await query.answer("Ошибка при загрузке товара.", show_alert=True)

    app.add_handler(CallbackQueryHandler(search_page_handler, pattern='^search:page:'))
    app.add_handler(CallbackQueryHandler(search_select_handler, pattern='^search:select:'))
    app.add_handler(CallbackQueryHandler(porder_confirm, pattern='^porder:confirm:'))
    app.add_handler(CallbackQueryHandler(porder_cancel, pattern='^porder:cancel$'))
    app.add_handler(CallbackQueryHandler(contact_admin_start, pattern='^contact:admin$'))