"""
Обработчик команды /sync — загружает все старые посты канала через Telethon.
"""
import asyncio
import logging
from telegram import Update
from telegram.ext import ContextTypes, CommandHandler
from telethon import TelegramClient
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
from bot.config import ADMIN_USER_ID, CHANNEL_ID, API_ID, API_HASH, SESSION_NAME
from bot.db import get_session, upsert_product
from bot.utils import parse_post_product

logger = logging.getLogger(__name__)


async def sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запускает фоновую синхронизацию старых постов."""
    if update.effective_user.id != ADMIN_USER_ID:
        await update.message.reply_text("❌ Нет доступа.")
        return

    await update.message.reply_text("⏳ Синхронизация старых постов запущена. Это может занять несколько минут...")

    # Запускаем асинхронную задачу, чтобы не блокировать бота
    context.application.create_task(
        sync_all_posts(context.bot, update.effective_chat.id)
    )


async def sync_all_posts(bot, admin_chat_id: int):
    """Обходит все сообщения канала и добавляет их в базу."""
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await client.start()

    try:
        channel = await client.get_entity(CHANNEL_ID)
        count = 0
        async for message in client.iter_messages(channel, limit=None):
            # Извлекаем текст из сообщения или подписи (caption)
            text = message.text or message.caption
            if not text:
                continue

            name, article, price, category, description, stock = parse_post_product(text)
            if not name:
                continue

            # Принудительно делаем товар неактивным и с нулевым остатком
            if stock is None:
                stock = 0
            in_stock = False   # скрыт
            is_active = False

            # Сохраняем в БД
            async for session in get_session():
                await upsert_product(
                    session,
                    post_id=str(message.id),
                    name=name,
                    price=price,
                    photo_file_id=None,   # Telethon не может напрямую получить file_id бота
                    video_file_id=None,
                    article=article,
                    category=category,
                    description=description,
                    in_stock=in_stock,
                    stock=stock,
                )
                # принудительно обновим is_active, если upsert_product не перезаписал
                from bot.db import Product
                from sqlalchemy import select
                stmt = select(Product).where(Product.post_id == str(message.id))
                result = await session.execute(stmt)
                product = result.scalar_one_or_none()
                if product:
                    product.is_active = False
                    await session.commit()

            count += 1

        await bot.send_message(
            chat_id=admin_chat_id,
            text=f"✅ Синхронизация завершена. Добавлено товаров: {count}"
        )
    except Exception as e:
        logger.error(f"Ошибка синхронизации: {e}")
        await bot.send_message(
            chat_id=admin_chat_id,
            text=f"❌ Ошибка синхронизации: {e}"
        )
    finally:
        await client.disconnect()


def register(app):
    app.add_handler(CommandHandler('sync', sync_command))