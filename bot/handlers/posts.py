"""
handlers/posts.py — синхронизация постов канала с базой данных и обработка комментариев.
"""
import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginChannel,
    Update,
)
from telegram.ext import ContextTypes, MessageHandler, filters
from bot.config import ADMIN_CHAT_ID, DISCUSSION_GROUP_ID
from bot.db import (
    Comment, get_session, upsert_product, Product, PendingOrder, get_bot_setting,
    Order, OrderItem, get_or_create_draft, add_item_to_order
)
from bot.utils import parse_post_product, parse_quantity, format_cart
from bot.utils import upload_photo_to_max, upload_video_to_max

logger = logging.getLogger(__name__)

# Глобальный буфер для медиагрупп
MEDIA_BUFFER = {}


async def _sync_post(message, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает пост (новый или отредактированный), парсит и сохраняет товар."""
    content_text = message.text or message.caption
    logger.info(f"_sync_post: message_id={message.message_id}, text={content_text[:100] if content_text else 'None'}")
    if not content_text:
        logger.info("Пост без текста — пропускаем")
        return

    text = content_text
    name, article, price, category, description, stock = parse_post_product(text)
    logger.info(f"Парсинг: name={name}, article={article}, price={price}, category={category}, stock={stock}")

    if not article:
        logger.info("В посте нет артикула — пропускаем")
        return

    if name is None:
        logger.info("Парсер не вернул название — товар не будет добавлен")
        return

    # Определяем post_id: если это пересылка, берём ID исходного поста
    if message.forward_origin and hasattr(message.forward_origin, 'message_id'):
        post_id = str(message.forward_origin.message_id)
        logger.info(f"Пересылка: сохранён post_id = {post_id} (оригинальный)")
    else:
        post_id = str(message.message_id)
        logger.info(f"Оригинальный пост: сохранён post_id = {post_id}")

    # --- Получаем file_id фото и видео (только самые большие) ---
    photo_file_ids = None
    if message.photo:
        photo_file_ids = message.photo[-1].file_id  # только самое большое фото

    video_file_ids = None
    if message.video:
        video_file_ids = message.video.file_id  # видео – один объект

    # --- Загружаем медиа в Max (по одному токену) ---
    max_photo_ids = None
    if photo_file_ids:
        token = await upload_photo_to_max(photo_file_ids, context.bot)
        max_photo_ids = token if token else None

    max_video_ids = None
    if video_file_ids:
        token = await upload_video_to_max(video_file_ids, context.bot)
        max_video_ids = token if token else None

    sold_keywords = ["продано", "нет в наличии", "sold", "закончился", "продана", "продан"]
    in_stock = not any(word in text.lower() for word in sold_keywords)

    async for session in get_session():
        await upsert_product(
            session,
            post_id=post_id,
            name=name,
            price=price,
            photo_file_ids=photo_file_ids,
            max_photo_ids=max_photo_ids,
            video_file_ids=video_file_ids,
            max_video_ids=max_video_ids,
            article=article,
            category=category,
            description=description,
            in_stock=in_stock,
            stock=stock,
        )
        logger.info(f"product saved with post_id={post_id}, max_photo_ids={max_photo_ids}, max_video_ids={max_video_ids}")
        logger.info(f"Товар обновлён: {name} | арт={article} | цена={price}₽")


async def process_media_group(context: ContextTypes.DEFAULT_TYPE, group_id: str):
    """Обрабатывает собранную медиагруппу из канала."""
    await asyncio.sleep(1)
    buffer = context.bot_data.get('media_buffer', {})
    if group_id not in buffer:
        return
    data = buffer.pop(group_id)
    caption = data['caption']
    photos = data['photos']       # список file_id фото
    videos = data['videos']       # список file_id видео

    if not caption:
        logger.info("Медиагруппа без подписи — пропускаем")
        return

    name, article, price, category, description, stock = parse_post_product(caption)
    if not name or not article:
        logger.info("Медиагруппа не содержит названия и артикула")
        return

    post_id = str(data['message_ids'][0])

    max_photo_ids = None
    if photos:
        tokens = []
        for file_id in photos:
            token = await upload_photo_to_max(file_id, context.bot)
            if token:
                tokens.append(token)
        max_photo_ids = ",".join(tokens) if tokens else None

    # Загружаем видео в Max
    max_video_ids = None
    if videos:
        tokens = []
        for file_id in videos:
            token = await upload_video_to_max(file_id, context.bot)
            if token:
                tokens.append(token)
        max_video_ids = ",".join(tokens) if tokens else None

    photo_file_ids = ",".join(photos) if photos else None
    video_file_ids = ",".join(videos) if videos else None

    sold_keywords = ["продано", "нет в наличии", "sold", "закончился", "продана", "продан"]
    in_stock = not any(word in caption.lower() for word in sold_keywords)

    async for session in get_session():
        await upsert_product(
            session,
            post_id=post_id,
            name=name,
            price=price,
            photo_file_ids=photo_file_ids,
            max_photo_ids=max_photo_ids,
            video_file_ids=video_file_ids,
            max_video_ids=max_video_ids,
            article=article,
            category=category,
            description=description,
            in_stock=in_stock,
            stock=stock,
        )
        logger.info(f"product saved with post_id={post_id}, max_photo_ids={max_photo_ids}, max_video_ids={max_video_ids}")
        logger.info(f"Товар из альбома обновлён: {name} | арт={article} | цена={price}₽")


async def catch_all_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех канальных постов с поддержкой медиагрупп."""
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return

    # Одиночное сообщение (не альбом) – обрабатываем сразу
    if not msg.media_group_id:
        await _sync_post(msg, context)
        return

    # Медиагруппа (альбом)
    group_id = msg.media_group_id
    if 'media_buffer' not in context.bot_data:
        context.bot_data['media_buffer'] = {}
    buffer = context.bot_data['media_buffer']

    if group_id not in buffer:
        buffer[group_id] = {
            'caption': msg.caption or '',
            'photos': [],
            'videos': [],
            'message_ids': [msg.message_id]
        }
    else:
        data = buffer[group_id]
        if msg.caption:
            data['caption'] = msg.caption
        data['message_ids'].append(msg.message_id)

    data = buffer[group_id]
    if msg.photo:
        data['photos'].append(msg.photo[-1].file_id)
    elif msg.video:
        data['videos'].append(msg.video.file_id)

    # Отложенная обработка группы
    if '_media_tasks' not in context.bot_data:
        context.bot_data['_media_tasks'] = {}
    tasks = context.bot_data['_media_tasks']
    if group_id in tasks:
        tasks[group_id].cancel()
    tasks[group_id] = asyncio.create_task(
        process_media_group(context, group_id)
    )


def _resolve_post_id(message) -> str | None:
    """Определяет id поста КАНАЛА (Product.post_id) по комментарию в группе.

    Пробует несколько источников:
    1. forward_origin (если репост канала)
    2. reply_to_message.message_id (если это репост, но без forward_origin)
    3. message_thread_id (если используется форум)
    4. текст ссылки в сообщении (если пользователь вставил ссылку)
    """
    reply = message.reply_to_message
    if reply is not None:
        # Способ 1: forward_origin
        origin = getattr(reply, "forward_origin", None)
        if isinstance(origin, MessageOriginChannel):
            return str(origin.message_id)
        # Способ 2: если это просто репост (без forward_origin), пробуем message_id
        # Но это может быть id репоста в группе, не канала!
        # Проверяем, есть ли у reply атрибут forward_from_chat
        if hasattr(reply, "forward_from_chat") and reply.forward_from_chat:
            # Это репост из канала, но без forward_origin
            # Пробуем получить message_id из репоста (может не работать)
            pass
        # Способ 3: пробуем извлечь post_id из текста реплая (если это ссылка).
        # getattr: у репоста/медиа-сообщения атрибута text может не быть
        # вовсе — раньше здесь падал AttributeError и комментарий терялся.
        reply_text = getattr(reply, "text", None) or ""
        if reply_text:
            import re
            match = re.search(r'/(\d+)$', reply_text)
            if match:
                return match.group(1)
            match = re.search(r'/post/(\d+)', reply_text)
            if match:
                return match.group(1)
            # Если текст реплая — это просто число (и оно похоже на post_id)
            if reply_text.strip().isdigit():
                return reply_text.strip()
        # Способ 4: берём message_id реплая (как fallback)
        return str(reply.message_id)
    # Способ 5: если тред форума
    if message.message_thread_id:
        return str(message.message_thread_id)
    return None


async def _notify_admin_comment(context, sender, product, text: str) -> None:
    """Уведомляет администратора о новом комментарии к товару (plain text)."""
    if not ADMIN_CHAT_ID:
        return
    name = (f"@{sender.username}" if sender.username
            else (sender.full_name or f"ID {sender.id}"))
    admin_text = (
        f"💬 Комментарий к товару «{product.name}»\n"
        f"От: {name} (ID {sender.id})\n\n{text}"
    )
    try:
        # parse_mode не указываем — текст пользовательский, защищаемся от инъекций.
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_text)
        logger.info("comment: admin notified",
                    extra={"event": "comment", "product_id": product.id})
    except Exception as e:
        logger.warning(f"Не удалось уведомить администратора о комментарии: {e}")


async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает комментарии в группе обсуждения к постам канала."""
    message = update.message
    if not message or not message.text:
        return
    if str(message.chat_id) != str(DISCUSSION_GROUP_ID):
        return

    user_id = message.from_user.id
    text = message.text.strip()
    qty = parse_quantity(text)

    # Определяем post_id (используем ту же логику, что и в cart.py)
    def _resolve_post_id(msg):
        reply = msg.reply_to_message
        if reply is not None:
            origin = getattr(reply, "forward_origin", None)
            if isinstance(origin, MessageOriginChannel):
                return str(origin.message_id)
            reply_text = getattr(reply, "text", None) or ""
            if reply_text:
                import re
                match = re.search(r'/(\d+)$', reply_text)
                if match:
                    return match.group(1)
                match = re.search(r'/post/(\d+)', reply_text)
                if match:
                    return match.group(1)
                if reply_text.strip().isdigit():
                    return reply_text.strip()
            return str(reply.message_id)
        if message.message_thread_id:
            return str(message.message_thread_id)
        return None

    post_id = _resolve_post_id(message)

    logger.info(
        "comment received",
        extra={"event": "comment", "user_id": user_id,
               "chat_id": message.chat_id, "post_id": post_id,
               "text": text[:100], "qty": qty}
    )

    if not post_id:
        logger.warning("no post_id resolved for comment")
        return

    async for session in get_session():
        # 1. Поиск по post_id
        product = (await session.execute(
            select(Product).where(Product.post_id == post_id)
        )).scalar_one_or_none()

        # 2. Если не нашли, пробуем найти по артикулу из реплая
        if not product:
            reply = message.reply_to_message
            if reply:
                reply_text = (getattr(reply, "text", None)
                              or getattr(reply, "caption", None) or "")
                if reply_text:
                    from bot.utils import parse_post_product
                    name, article, _, _, _, _ = parse_post_product(reply_text)
                    if article:
                        product = (await session.execute(
                            select(Product).where(Product.article == article)
                        )).scalar_one_or_none()
                        if product:
                            product.post_id = str(post_id)
                            await session.commit()
                            logger.info("product found by article, updated post_id",
                                        extra={"article": article, "product_id": product.id})

        if not product:
            logger.warning("no product for post_id", extra={"post_id": post_id})
            return

        # 3. Сохраняем комментарий
        session.add(Comment(product_id=product.id, user_id=user_id, text=text))
        await session.commit()
        logger.info("comment saved", extra={"product_id": product.id})

        # 3.1 Уведомляем админа. _notify_admin_comment была написана, но НИ
        # разу не вызывалась: админ не видел ни вопросов, ни заявок из группы
        # обсуждения (комментарий без количества просто терялся в логах).
        # Внутри функции исключения перехватываются, поэтому на обработку
        # комментария это влиять не может.
        await _notify_admin_comment(context, message.from_user, product, text)

        # 4. Если нет количества — просто комментарий
        if qty is None:
            # Не пишем в группу, только логируем
            return

        # 5. Проверка активности товара
        if not product.is_active:
            logger.info("inactive product requested", extra={"product_id": product.id})
            return

        # 6. Проверка наличия QR-кода (Telegram file_id)
        qr_telegram = await get_bot_setting(session, "payment_qr_telegram")
        if not qr_telegram:
            logger.warning("payment QR not set, cannot process order")
            return

        # 7. Добавление в корзину и отправка/обновление подтверждения
        try:
            order = await get_or_create_draft(session, user_id)
            stmt_order = select(Order).where(Order.id == order.id).options(
                selectinload(Order.items).selectinload(OrderItem.product)
            )
            order = (await session.execute(stmt_order)).scalar_one()

            await add_item_to_order(session, order, product, qty)
            order = (await session.execute(stmt_order)).scalar_one()

            cart_text = format_cart(order)
            text_msg = (
                f"🛒 **Ваша корзина:**\n\n"
                f"{cart_text}\n\n"
                f"Подтвердить заказ?"
            )

            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить заказ", callback_data="porder:confirm")],
                [InlineKeyboardButton("❌ Отменить заказ", callback_data="porder:cancel")]
            ])

            # Проверяем существующую запись PendingOrder
            stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
            existing_pending = (await session.execute(stmt)).scalar_one_or_none()

            if existing_pending and existing_pending.confirmation_msg_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=existing_pending.confirmation_msg_id,
                        text=text_msg,
                        reply_markup=kb,
                        parse_mode="Markdown"
                    )
                    existing_pending.product_id = product.id
                    existing_pending.quantity = qty
                    await session.commit()
                    logger.info("confirmation message updated", extra={"user_id": user_id})
                    await message.delete()
                    return
                except Exception as e:
                    logger.warning(f"Failed to edit message: {e}")
                    await session.delete(existing_pending)
                    await session.commit()
                    # Продолжим и создадим новое сообщение
            else:
                if existing_pending:
                    await session.delete(existing_pending)
                    await session.commit()

            sent = await context.bot.send_message(
                chat_id=user_id,
                text=text_msg,
                reply_markup=kb,
                parse_mode="Markdown"
            )

            pending = PendingOrder(
                user_id=user_id,
                product_id=product.id,
                quantity=qty,
                confirmation_msg_id=sent.message_id,
            )
            session.add(pending)
            await session.commit()

            logger.info("new confirmation sent", extra={"user_id": user_id})
            await message.delete()
            return

        except Exception as e:
            logger.warning(f"Failed to process order: {e}")
            await session.rollback()
            # Сохраняем PendingOrder без ID сообщения, чтобы пользователь мог подтвердить через /start
            stmt = select(PendingOrder).where(PendingOrder.user_id == user_id)
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing:
                existing.product_id = product.id
                existing.quantity = qty
            else:
                pending = PendingOrder(user_id=user_id, product_id=product.id, quantity=qty)
                session.add(pending)
            await session.commit()
            # Не пишем в группу, только логируем
            await message.delete()
            return


def register(app):
    # Универсальный обработчик для канала
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST | filters.UpdateType.EDITED_CHANNEL_POST,
        catch_all_channel_posts
    ))
    # Обработчик комментариев в группе обсуждения (если задан ID).
    # Регистрируем в более ранней группе (-1), чем прикладные хэндлеры (группа 0),
    # чтобы гарантированно обработать комментарий первым и не зависеть от порядка
    # регистрации модулей в main.py. Фильтр ограничен именно группой обсуждения.
    if DISCUSSION_GROUP_ID:
        app.add_handler(
            MessageHandler(
                filters.Chat(DISCUSSION_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
                handle_comment,
            ),
            group=-1,
        )
    else:
        logger.warning(
            "DISCUSSION_GROUP_ID не задан — обработка комментариев отключена",
            extra={"event": "comment_setup"},
        )