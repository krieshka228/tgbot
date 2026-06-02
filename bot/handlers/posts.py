"""
handlers/posts.py — синхронизация постов канала с базой данных.
Поддерживает одиночные фото/видео и медиагруппы (альбомы).
"""
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
from bot.config import CHANNEL_ID
from bot.db import get_session, upsert_product
from bot.utils import parse_post_product

logger = logging.getLogger(__name__)

# Глобальный буфер для незавершённых медиагрупп
MEDIA_BUFFER = {}  # ключ: media_group_id, значение: dict с caption, фото, видео, message_ids


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
    if name is None:
        logger.info("Парсер не вернул название — товар не будет добавлен")
        return

    post_id = str(message.message_id)

    sold_keywords = ["продано", "нет в наличии", "sold", "закончился", "продана", "продан"]
    in_stock = not any(word in text.lower() for word in sold_keywords)

    photo_file_ids = None
    if message.photo:
        # Берём только самое большое изображение
        photo_file_ids = message.photo[-1].file_id
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
    await asyncio.sleep(1)  # даём время собраться всем частям
    if group_id not in MEDIA_BUFFER:
        return
    data = MEDIA_BUFFER.pop(group_id)
    caption = data['caption']
    photos = data['photos']   # список file_id
    videos = data['videos']   # список file_id

    if not caption:
        logger.info("Медиагруппа без подписи — пропускаем")
        return

    name, article, price, category, description, stock = parse_post_product(caption)
    if not name:
        logger.info("Парсер не смог распознать товар из медиагруппы")
        return

    post_id = str(data['message_ids'][0])  # ID первого сообщения
    sold_keywords = ["продано", "нет в наличии", "sold", "закончился", "продана", "продан"]
    in_stock = not any(word in caption.lower() for word in sold_keywords)

    # Объединяем фото и видео
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
        logger.info(f"Товар из альбома обновлён: {name} | арт={article} | цена={price}₽ | "
                    f"фото={len(photos)} видео={len(videos)}")


async def catch_all_channel_posts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик всех канальных постов с поддержкой медиагрупп."""
    msg = update.channel_post or update.edited_channel_post
    if not msg:
        return

    # Если сообщение входит в медиагруппу
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
            # caption берём из первого сообщения, где он был
            if msg.caption:
                data['caption'] = msg.caption
            data['message_ids'].append(msg.message_id)

        data = MEDIA_BUFFER[group_id]
        if msg.photo:
            data['photos'].append(msg.photo[-1].file_id)
        elif msg.video:
            data['videos'].append(msg.video.file_id)

        # Запускаем отложенную обработку (перезапускаем таймер при каждом новом сообщении)
        if hasattr(context.application, '_media_tasks'):
            if group_id in context.application._media_tasks:
                context.application._media_tasks[group_id].cancel()
        else:
            context.application._media_tasks = {}
        context.application._media_tasks[group_id] = asyncio.create_task(
            process_media_group(context, group_id)
        )
        return

    # Одиночное сообщение
    await _sync_post(msg, context)


def register(app):
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST | filters.UpdateType.EDITED_CHANNEL_POST,
        catch_all_channel_posts
    ))