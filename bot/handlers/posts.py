"""
handlers/posts.py — синхронизация постов канала с базой данных и обработка комментариев.
"""
import asyncio
import logging
from sqlalchemy import select
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MessageOriginChannel,
    Update,
)
from telegram.ext import ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from bot.config import ADMIN_CHAT_ID, CHANNEL_ID, DISCUSSION_GROUP_ID
from bot.db import Comment, get_session, upsert_product, Product, PendingOrder, get_bot_setting
from bot.utils import parse_post_product, parse_quantity, escape_markdown
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
        # Способ 3: пробуем извлечь post_id из текста реплая (если это ссылка)
        if reply.text:
            import re
            match = re.search(r'/(\d+)$', reply.text)
            if match:
                return match.group(1)
            match = re.search(r'/post/(\d+)', reply.text)
            if match:
                return match.group(1)
            # Если текст реплая — это просто число (и оно похоже на post_id)
            if reply.text.strip().isdigit():
                return reply.text.strip()
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
    message = update.message
    if not message or not message.text:
        return
    if str(message.chat_id) != str(DISCUSSION_GROUP_ID):
        return

    user_id = message.from_user.id
    text = message.text.strip()
    qty = parse_quantity(text)

    # Используем единую функцию для определения post_id
    post_id = _resolve_post_id(message)

    logger.info(
        "comment received",
        extra={"event": "comment", "user_id": user_id,
               "chat_id": message.chat_id, "post_id": post_id,
               "text": text[:100], "qty": qty}
    )

    if not post_id:
        await message.reply_text("❌ Не удалось определить пост.")
        return

    async for session in get_session():
        # 1. Поиск по post_id
        product = (await session.execute(
            select(Product).where(Product.post_id == post_id)
        )).scalar_one_or_none()

        # 2. Если не нашли, пробуем найти по артикулу из реплая (text или caption)
        if not product:
            reply = message.reply_to_message
            if reply:
                reply_text = reply.text or reply.caption or ""
                if reply_text:
                    from bot.utils import parse_post_product
                    name, article, _, _, _, _ = parse_post_product(reply_text)
                    logger.debug(f"Extracted article from reply: {article}")
                    if article:
                        product = (await session.execute(
                            select(Product).where(Product.article == article)
                        )).scalar_one_or_none()
                        if product:
                            product.post_id = str(post_id)
                            await session.commit()
                            logger.info("product found by article, updated post_id",
                                        extra={"article": article, "product_id": product.id})
                        else:
                            logger.warning(f"Product with article {article} not found")
                    else:
                        logger.debug("No article extracted from reply text")
                else:
                    logger.debug("No reply text or caption to extract article")

        if not product:
            await message.reply_text("❌ Товар не найден. Возможно, пост был добавлен до синхронизации.")
            logger.warning("no product for post_id", extra={"post_id": post_id})
            return

        # 3. Сохраняем комментарий
        from bot.db import Comment
        session.add(Comment(product_id=product.id, user_id=user_id, text=text))
        await session.commit()
        logger.info("comment saved", extra={"product_id": product.id})

        # 4. Если нет количества — просто комментарий
        if qty is None:
            await message.reply_text("💬 Спасибо за комментарий! Чтобы заказать, напишите количество.")
            return

        # 5. Проверка активности
        if not product.is_active:
            await message.reply_text("❌ Этот товар временно недоступен.")
            return

        # 6. Проверка QR
        qr_token = await get_bot_setting(session, "payment_qr_token")
        if not qr_token:
            await message.reply_text("⚠️ Оплата временно недоступна. Попробуйте позже.")
            return

        # 7. Проверка остатка
        if product.stock is not None and qty > product.stock:
            await message.reply_text(
                f"❌ Недостаточно товара. Доступно только {product.stock} шт."
            )
            logger.info("not enough stock",
                        extra={"product_id": product.id, "qty": qty, "stock": product.stock})
            return

        # 8. Отправка подтверждения в личку
        try:
            total = product.price * qty
            from bot.utils import escape_markdown
            text_msg = (
                f"🛒 **Ваш заказ:**\n"
                f"• {escape_markdown(product.name)} — {qty} шт. × {product.price:.0f} ₽ = {total:.0f} ₽\n\n"
                f"Подтвердить заказ?"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить", callback_data=f"porder:confirm:{product.id}:{qty}")],
                [InlineKeyboardButton("❌ Отменить", callback_data="porder:cancel")]
            ])
            await context.bot.send_message(
                chat_id=user_id,
                text=text_msg,
                reply_markup=kb,
                parse_mode="Markdown"
            )
            logger.info("confirmation sent to DM", extra={"user_id": user_id, "product_id": product.id})
            await message.delete()
            return
        except Exception as e:
            logger.warning(f"DM failed for user {user_id}: {e}")
            pending = PendingOrder(user_id=user_id, product_id=product.id, quantity=qty)
            await session.merge(pending)
            await session.commit()
            await message.reply_text(
                f"✅ Ваш заказ на {product.name} (×{qty}) принят. Напишите /start, чтобы подтвердить."
            )
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