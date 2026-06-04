import logging
import uuid
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputMediaVideo
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from bot.db import get_session, get_or_create_user, get_order_with_items, OrderStatus, Product, Order, OrderItem
from bot.keyboards import kb_main_menu, kb_back_to_menu, kb_cart_actions, kb_admin_menu, reply_main_menu
from bot.config import ADMIN_USER_ID, ADMIN_CHAT_ID
from bot.utils import parse_quantity, _parse_post_link, format_cart, parse_post_product, escape_markdown
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from bot.db import upsert_product
import asyncio
from bot.db import set_bot_setting




MEDIA_GROUP_TIMEOUT = 2

logger = logging.getLogger(__name__)


# ================== Обработчики состояний ==================


async def process_admin_sync_with_media(message, caption, context, photos, videos):
    """Обрабатывает медиагруппу (альбом), собранную collect_media_group."""
    if not caption:
        await message.reply_text("❌ Альбом без подписи. Добавьте текст к первому фото.")
        return
    # Вызываем основной обработчик, передавая фото/видео
    await process_admin_sync(message, caption, context, photos=photos, videos=videos)

async def collect_media_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Собирает все сообщения из одной медиагруппы, возвращает (caption, [photo_file_ids], [video_file_ids])."""
    message = update.message
    group_id = message.media_group_id
    # Инициализируем буфер в user_data, если ещё нет
    if 'media_group_buffer' not in context.user_data:
        context.user_data['media_group_buffer'] = {}
    buffer = context.user_data['media_group_buffer']

    if group_id not in buffer:
        buffer[group_id] = {
            'caption': message.caption or '',
            'photos': [],
            'videos': [],
            'messages': [message.message_id]
        }
    else:
        entry = buffer[group_id]
        if message.caption:
            entry['caption'] = message.caption  # caption может быть только у первого, но на всякий случай
        entry['messages'].append(message.message_id)

    entry = buffer[group_id]
    if message.photo:
        entry['photos'].append(message.photo[-1].file_id)
    elif message.video:
        entry['videos'].append(message.video.file_id)

    # Ждём заданное время, если группа ещё не полная (можно проверять по количеству, но мы просто таймаут)
    await asyncio.sleep(MEDIA_GROUP_TIMEOUT)
    # Если за время ожидания не добавились новые сообщения, считаем группу завершённой
    # (простая эвристика: если длина messages не изменилась)
    current_len = len(entry['messages'])
    await asyncio.sleep(0.5)  # ещё чуть-чуть на всякий случай
    if len(entry['messages']) == current_len:
        # Удаляем из буфера и возвращаем результат
        del buffer[group_id]
        return entry['caption'], entry['photos'], entry['videos'], entry['messages']
    else:
        # Если добавились новые сообщения, повторно вызываем функцию, но проще вернуть None
        # Для простоты будем обрабатывать в следующем вызове message_dispatcher
        return None

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
        if not user.consented:
            await message.reply_text("❌ Сначала дайте согласие: /start", reply_markup=kb_back_to_menu())
            context.user_data.pop('state', None)
            return True
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
        order = (await session.execute(stmt)).scalar_one()
        cart_text = f"✅ **{product.name}** × {qty} шт. добавлен в корзину!\n\n{format_cart(order)}"

    if card_msg_id:
        try:
            await context.bot.edit_message_text(cart_text,
                                                chat_id=message.chat_id,
                                                message_id=card_msg_id,
                                                reply_markup=kb_cart_actions(order.id),
                                                parse_mode="Markdown")
        except Exception:
            await message.reply_text(cart_text, reply_markup=kb_cart_actions(order.id), parse_mode="Markdown")
    else:
        await message.reply_text(cart_text, reply_markup=kb_cart_actions(order.id), parse_mode="Markdown")
    context.user_data.pop('state', None)
    return True


async def process_cart_change_qty(message, text, context):
    data = context.user_data.get('data', {})
    item_id = data.get('item_id')
    if not item_id:
        await message.reply_text("❌ Ошибка. Попробуйте снова.")
        context.user_data.pop('state', None)
        return True
    try:
        new_qty = int(text.strip())
        if new_qty <= 0:
            raise ValueError
    except ValueError:
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
                             parse_mode="Markdown",
                             reply_markup=kb_cart_actions(order.id))
    context.user_data.pop('state', None)
    return True


async def process_search_article(message, text, context):
    article = text.strip()
    if not article:
        await message.reply_text("❌ Введите артикул.", reply_markup=kb_back_to_menu())
        return True
    async for session in get_session():
        product = (await session.execute(
            select(Product).where(Product.is_active == True, Product.article == article)
        )).scalar_one_or_none()
        break
    if not product:
        await message.reply_text("🔎 Товар с таким артикулом не найден.", reply_markup=kb_back_to_menu())
        context.user_data.pop('state', None)
        return True

    # Экранируем спецсимволы
    name = escape_markdown(product.name)
    article_str = escape_markdown(product.article) if product.article else None
    msg_text = name
    if article_str:
        msg_text += f"\nАртикул {article_str}"
    if product.stock is not None:
        msg_text += f"\nНа складе: {product.stock}"
    msg_text += f"\n\nЦена {product.price:.0f} ₽"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Заказать", callback_data=f"order:start:{product.id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])

    photos = product.photo_file_ids.split(',') if product.photo_file_ids else []
    videos = product.video_file_ids.split(',') if product.video_file_ids else []

    try:
        if len(photos) == 1 and not videos:
            await message.reply_photo(photo=photos[0], caption=msg_text, reply_markup=kb, parse_mode="Markdown")
        elif len(videos) == 1 and not photos:
            await message.reply_video(video=videos[0], caption=msg_text, reply_markup=kb, parse_mode="Markdown")
        elif photos or videos:
            media = []
            for idx, fid in enumerate(photos):
                if idx == 0 and not videos:
                    media.append(InputMediaPhoto(media=fid, caption=msg_text, parse_mode="Markdown"))
                else:
                    media.append(InputMediaPhoto(media=fid))
            for idx, fid in enumerate(videos):
                if idx == 0 and not photos:
                    media.append(InputMediaVideo(media=fid, caption=msg_text, parse_mode="Markdown"))
                else:
                    media.append(InputMediaVideo(media=fid))
            if photos and videos:
                media[0] = InputMediaPhoto(media=photos[0], caption=msg_text, parse_mode="Markdown")

            msgs = await message.reply_media_group(media=media)
            await message.reply_text("Выберите действие:", reply_markup=kb)
        else:
            await message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Ошибка отправки медиа при поиске артикула {article}: {e}")
        await message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")

    context.user_data.pop('state', None)
    return True


async def process_awaiting_phone(message, text, context):
    phone = text.strip()
    if not phone or not phone.replace('+', '').replace(' ', '').isdigit():
        await message.reply_text("❌ Введите корректный номер телефона (цифры, можно с +).")
        return True

    user_id = message.from_user.id
    order_id = None

    # Сначала проверяем, есть ли уже данные в user_data
    data = context.user_data.get('data', {})
    if data.get('order_id'):
        order_id = data['order_id']
    else:
        # Иначе ищем подтверждённый заказ без телефона
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

    # Сохраняем телефон
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

    # Переходим к выбору доставки
    context.user_data['state'] = 'awaiting_delivery_method'
    context.user_data['data'] = {'order_id': order_id}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Озон", callback_data="delivery:ozon"),
         InlineKeyboardButton("Яндекс", callback_data="delivery:yandex")],
        [InlineKeyboardButton("СДЭК до ПВЗ", callback_data="delivery:cdek")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])
    await message.reply_text("🚚 Выберите способ доставки:", reply_markup=kb)
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
        # Готовим данные для подтверждения
        phone = user.phone or "не указан"
        method = order.delivery_method or "не выбран"
        total = order.total_amount
    # Переход к подтверждению
    context.user_data['state'] = 'confirm_customer_data'
    summary = (
        f"📋 **Проверьте данные заказа:**\n\n"
        f"📱 Телефон: {phone}\n"
        f"🚚 Доставка: {method}\n"
        f"📍 Адрес: {address}\n"
        f"💰 Сумма: {total:.0f} ₽"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_data:yes")],
        [InlineKeyboardButton("✏️ Изменить адрес", callback_data="confirm_data:edit_address")],
        [InlineKeyboardButton("📱 Изменить телефон", callback_data="confirm_data:edit_phone")],
    ])
    await message.reply_text(summary, reply_markup=kb, parse_mode="Markdown")
    return True


# ================== Обработчики подтверждения данных ==================

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
        admin_text = f"📦 **Заказ #{order_id} готов к отправке**\n\n" \
                     f"👤 Клиент: {user.full_name or 'Без имени'} (ID {user.id})\n" \
                     f"📱 Телефон: {user.phone or 'не указан'}\n" \
                     f"🚚 Доставка: {order.delivery_method}\n" \
                     f"📍 Адрес: {order.delivery_address}\n\n" \
                     f"💰 Итого: {order.total_amount:.0f} ₽"
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text, parse_mode="Markdown")
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


# ================== Остальные обработчики состояний ==================

async def process_contact_admin(message, text, context):
    if not text.strip():
        await message.reply_text("Напишите ваш вопрос текстом.")
        return True
    sender_name = message.from_user.full_name or f"ID {message.from_user.id}"
    fwd_text = f"✉️ **Сообщение от клиента**\nКлиент: {sender_name} (ID: {message.from_user.id})\n\n{text.strip()}"
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=fwd_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Не удалось переслать сообщение: {e}")
    context.user_data.pop('state', None)
    await message.reply_text("✅ Сообщение отправлено администратору!",
                             reply_markup=kb_main_menu(is_admin=(message.from_user.id == ADMIN_USER_ID)))
    return True


async def process_admin_sync(message, text, context, photos=None, videos=None):
    """Обрабатывает одиночное сообщение (или уже собранную медиагруппу)."""
    if message.from_user.id != ADMIN_USER_ID:
        return True
    if not text or not text.strip():
        if message.photo or message.video:
            await message.reply_text("❌ Для синхронизации нужен текст. Перешлите пост с подписью или скопируйте текст.")
        else:
            await message.reply_text("❌ Не удалось получить текст из сообщения.")
        return True

    name, article, price, category, description, stock = parse_post_product(text)
    if not name:
        await message.reply_text("❌ Не удалось распознать товар. Текст:\n" + text[:200])
        return True

    # Проверка на дубликат
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
            await message.reply_text(f"⚠️ Товар «{name}» уже существует (арт. {article or '—'}). Пропущен.")
            context.user_data['sync_skipped'] = context.user_data.get('sync_skipped', 0) + 1
            return True

    if stock is None:
        stock = 0
    post_id = f"manual_{uuid.uuid4().hex[:8]}"

    # Собираем фото/видео из переданных списков или из самого сообщения
    if photos is None:
        photos = [message.photo[-1].file_id] if message.photo else []
    if videos is None:
        videos = [message.video.file_id] if message.video else []
    photo_file_ids = ",".join(photos) if photos else None
    video_file_ids = ",".join(videos) if videos else None

    async for session in get_session():
        await upsert_product(
            session, post_id, name, price,
            photo_file_ids=photo_file_ids,
            video_file_ids=video_file_ids,
            article=article, category=category,
            description=description, in_stock=False, stock=stock,
        )
        from bot.db import Product as DBProduct
        stmt = select(DBProduct).where(DBProduct.post_id == post_id)
        result = await session.execute(stmt)
        product = result.scalar_one_or_none()
        if product:
            product.is_active = False
            await session.commit()

    try:
        await message.delete()
    except Exception:
        pass

    # Удаляем предыдущее сообщение с кнопкой, если было
    last_msg_id = context.user_data.get('last_sync_msg_id')
    if last_msg_id:
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=last_msg_id)
        except Exception:
            pass

    sent = await message.reply_text(
        f"✅ Добавлен товар: «{name}» (скрыт, остаток 0)",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]])
    )
    context.user_data['last_sync_msg_id'] = sent.message_id
    context.user_data['sync_count'] = context.user_data.get('sync_count', 0) + 1
    return True


async def process_admin_set_stock(message, text, context):
    if message.from_user.id != ADMIN_USER_ID:
        return True
    data = context.user_data.get('data', {})
    product_id = data.get('product_id')
    try:
        new_stock = int(text.strip())
        if new_stock < 0:
            raise ValueError
    except ValueError:
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
        await message.reply_text(f"✅ Остаток товара «{product.name}» обновлён: {new_stock}", reply_markup=kb_admin_menu())
    context.user_data.pop('state', None)
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
        if not user.consented:
            await message.reply_text("❌ Сначала нужно дать согласие. Нажмите /start")
            return True
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
                             parse_mode="Markdown",
                             reply_markup=kb_cart_actions(order.id))
    return True


# ================== Единый диспетчер сообщений ==================

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
                await set_bot_setting(session, "payment_qr_token", file_id)
            context.user_data.pop('state', None)
            await message.reply_text("✅ QR-код сохранён. Теперь он будет показываться покупателям при оформлении заказа.",
                                     reply_markup=kb_main_menu(is_admin=True))
        else:
            await message.reply_text("❌ Пришлите изображение в формате PNG.", reply_markup=kb_back_to_menu())
        return

    if state == 'admin_sync':
        if message.media_group_id:
            result = await collect_media_group(update, context)
            if result is None:
                return  # ждём остальные части альбома
            caption, photos, videos, msg_ids = result
            # Удаляем все части альбома, кроме первой (или все, но тогда теряем caption)
            # Проще передать данные в process_admin_sync_with_media, а исходные сообщения удалить
            # Создадим фейковое сообщение, чтобы передать информацию о пользователе
            # Для простоты вызовем process_admin_sync_with_media напрямую
            await process_admin_sync_with_media(message, caption, context, photos, videos)
            # Удаляем все части медиагруппы
            for mid in msg_ids:
                try:
                    await context.bot.delete_message(chat_id=message.chat_id, message_id=mid)
                except Exception:
                    pass
            return
        else:
            # Одиночное сообщение (текст или фото с подписью)
            await process_admin_sync(message, content_text, context)
        return

    if not content_text:
        return

    if state == 'order_qty':
        await process_order_qty(message, content_text, context)
    elif state == 'cart_change_qty':
        await process_cart_change_qty(message, content_text, context)
    elif state == 'search_article':
        await process_search_article(message, content_text, context)
    elif state == 'awaiting_phone':
        await process_awaiting_phone(message, content_text, context)
    elif state == 'awaiting_address':
        await process_awaiting_address(message, content_text, context)
    elif state == 'contact_admin':
        await process_contact_admin(message, content_text, context)
    elif state == 'admin_set_stock':
        await process_admin_set_stock(message, content_text, context)
    else:
        # Проверяем, не ожидается ли телефон для подтверждённого заказа
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
        # Если ничего не подошло, пробуем прямой заказ
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


def register(app):
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION | filters.PHOTO | filters.VIDEO) & ~filters.ChatType.CHANNEL,
        message_dispatcher
    ))
    app.add_handler(CallbackQueryHandler(handle_delivery_choice, pattern='^delivery:'))
    # Новые обработчики подтверждения данных
    app.add_handler(CallbackQueryHandler(confirm_data_yes, pattern='^confirm_data:yes$'))
    app.add_handler(CallbackQueryHandler(confirm_data_edit_address, pattern='^confirm_data:edit_address$'))
    app.add_handler(CallbackQueryHandler(confirm_data_edit_phone, pattern='^confirm_data:edit_phone$'))