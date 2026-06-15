"""
handlers/posts.py — синхронизация постов канала с базой данных и обработка комментариев.
"""
import asyncio
import logging
from sqlalchemy import select
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes, MessageHandler, filters
from telegram.constants import ParseMode
from bot.config import CHANNEL_ID, DISCUSSION_GROUP_ID
from bot.db import get_session, upsert_product, Product, PendingOrder, get_bot_setting
from bot.utils import parse_post_product, parse_quantity

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

    # --- Игнорируем посты без артикула ---
    if not article:
        logger.info("В посте нет артикула — пропускаем")
        return

    if name is None:
        logger.info("Парсер не вернул название — товар не будет добавлен")
        return

    post_id = str(message.message_id)

    sold_keywords = ["продано", "нет в наличии", "sold", "закончился", "продана", "продан"]
    in_stock = not any(word in text.lower() for word in sold_keywords)

    photo_file_ids = None
    if message.photo:
        photo_file_ids = ",".join([photo.file_id for photo in message.photo])
    video_file_ids = None
    if message.video:
        video_file_ids = message.video.file_id

    async for session in get_session():
        await upsert_product(
            session,
            post_id=post_id,
            name=name,
            price=price,
            photo_file_ids=photo_file_ids,
            video_file_ids=video_file_ids,
            article=article,
            category=category,
            description=description,
            in_stock=in_stock,
            stock=stock,
        )
        logger.info(f"Товар обновлён: {name} | арт={article} | цена={price}₽")


async def process_media_group(context: ContextTypes.DEFAULT_TYPE, group_id: str):
    """Обрабатывает собранную медиагруппу из канала."""
    await asyncio.sleep(1)
    if group_id not in MEDIA_BUFFER:
        return
    data = MEDIA_BUFFER.pop(group_id)
    caption = data['caption']
    photos = data['photos']
    videos = data['videos']

    if not caption:
        logger.info("Медиагруппа без подписи — пропускаем")
        return

    name, article, price, category, description, stock = parse_post_product(caption)
    if not name or not article:
        logger.info("Медиагруппа не содержит названия и артикула")
        return

    post_id = str(data['message_ids'][0])
    sold_keywords = ["продано", "нет в наличии", "sold", "закончился", "продана", "продан"]
    in_stock = not any(word in caption.lower() for word in sold_keywords)

    photo_file_ids = ",".join(photos) if photos else None
    video_file_ids = ",".join(videos) if videos else None

    async for session in get_session():
        await upsert_product(
            session,
            post_id=post_id,
            name=name,
            price=price,
            photo_file_ids=photo_file_ids,
            video_file_ids=video_file_ids,
            article=article,
            category=category,
            description=description,
            in_stock=in_stock,
            stock=stock,
        )
        logger.info(f"Товар из альбома обновлён: {name} | арт={article} | цена={price}₽")


async def catch_all_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех канальных постов с поддержкой медиагрупп."""
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return

    if msg.media_group_id:
        group_id = msg.media_group_id
        if group_id not in MEDIA_BUFFER:
            MEDIA_BUFFER[group_id] = {
                'caption': msg.caption or '',
                'photos': [],
                'videos': [],
                'message_ids': [msg.message_id]
            }
        else:
            data = MEDIA_BUFFER[group_id]
            if msg.caption:
                data['caption'] = msg.caption
            data['message_ids'].append(msg.message_id)

        data = MEDIA_BUFFER[group_id]
        if msg.photo:
            data['photos'].append(msg.photo[-1].file_id)
        elif msg.video:
            data['videos'].append(msg.video.file_id)

        if hasattr(context.application, '_media_tasks'):
            if group_id in context.application._media_tasks:
                context.application._media_tasks[group_id].cancel()
        else:
            context.application._media_tasks = {}
        context.application._media_tasks[group_id] = asyncio.create_task(
            process_media_group(context, group_id)
        )
        return

    await _sync_post(msg, context)


# ================== Обработчик комментариев ==================
async def handle_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    logger.info(f"handle_comment вызван! chat_id={message.chat_id}, text={message.text}")
    if not message or not message.text:
        logger.info("handle_comment: сообщение пустое — выход")
        return
    if str(message.chat_id) != str(DISCUSSION_GROUP_ID):
        logger.info(f"handle_comment: chat_id не совпадает с DISCUSSION_GROUP_ID ({DISCUSSION_GROUP_ID}) — выход")
        return

    user_id = message.from_user.id
    text = message.text.strip()
    qty = parse_quantity(text)
    if qty is None:
        logger.info("handle_comment: не удалось извлечь количество — выход")
        return

    # Определяем post_id
    # Определяем post_id (ID оригинального поста в канале)
    post_id = None
    if message.reply_to_message:
        # Пытаемся получить ID оригинального поста канала через forward_origin
        if message.reply_to_message.forward_origin and hasattr(message.reply_to_message.forward_origin, 'message_id'):
            post_id = str(message.reply_to_message.forward_origin.message_id)
            logger.info(f"handle_comment: forward_origin.message_id={post_id}")
        else:
            # Если forward_origin нет, возможно, ответ на обычное сообщение группы (не из канала)
            post_id = str(message.reply_to_message.message_id)
            logger.info(f"handle_comment: reply_to_message.message_id={post_id}")
    elif message.is_topic_message and message.message_thread_id:
        post_id = str(message.message_thread_id)
        logger.info(f"handle_comment: is_topic_message, post_id={post_id}")
    else:
        logger.info("handle_comment: не reply_to_message и не топик — выход")
        return

    if not post_id:
        logger.info("handle_comment: post_id пустой — выход")
        return

    async for session in get_session():
        product = (await session.execute(
            select(Product).where(Product.post_id == post_id, Product.is_active == True)
        )).scalar_one_or_none()
        if not product:
            logger.info(f"handle_comment: товар с post_id={post_id} не найден или неактивен — выход")
            return

        qr_token = await get_bot_setting(session, "payment_qr_token")
        if not qr_token:
            logger.info("handle_comment: QR-код не задан — удаляем комментарий и выходим")
            try:
                await message.delete()
            except Exception:
                pass
            return

        if product.stock is not None and qty > product.stock:
            logger.info("handle_comment: недостаточно товара — удаляем комментарий и выходим")
            try:
                await message.delete()
            except Exception:
                pass
            return

        try:
            total = product.price * qty
            text_msg = (
                f"🛒 **Ваш заказ:**\n"
                f"• {product.name} — {qty} шт. × {product.price:.0f} ₽ = {total:.0f} ₽\n\n"
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
                parse_mode=ParseMode.MARKDOWN
            )
            logger.info("handle_comment: сообщение отправлено клиенту, удаляем комментарий")
            await message.delete()
            return
        except Exception as e:
            logger.info(f"Не удалось отправить ЛС пользователю {user_id}: {e}")
            existing = await session.get(PendingOrder, user_id)
            if existing:
                existing.product_id = product.id
                existing.quantity = qty
            else:
                pending = PendingOrder(user_id=user_id, product_id=product.id, quantity=qty)
                session.add(pending)
            await session.commit()
            logger.info("handle_comment: отложенный заказ сохранён, удаляем комментарий")
            await message.delete()
            return

def register(app):
    # Универсальный обработчик для канала
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST | filters.UpdateType.EDITED_CHANNEL_POST,
        catch_all_channel_posts
    ))
    # Обработчик комментариев в группе обсуждения (если задан ID)
    if DISCUSSION_GROUP_ID:
        app.add_handler(MessageHandler(
            filters.Chat(DISCUSSION_GROUP_ID) & filters.TEXT & ~filters.COMMAND,
            handle_comment
        ))