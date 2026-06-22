"""
utils.py — Вспомогательные функции для парсинга постов, форматирования корзины и т.д.
"""

import re
from typing import Optional
import logging
from telegram import Bot as TelegramBot
from aiomax import Bot as MaxBot
import io
import aiohttp
from bot.config import settings

MAX_API_BASE = "https://platform-api.max.ru"

logger = logging.getLogger(__name__)

def parse_quantity(text: str) -> Optional[int]:
    """Извлекает целое количество из текста (например, '3 шт', '5')."""
    if not text:
        return None
    match = re.search(r'\d+', text)
    return int(match.group()) if match else None


def escape_markdown(text: str) -> str:
    """Экранирует специальные символы Markdown."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)


def parse_post_product(text: str) -> tuple:
    if not text:
        return (None, None, 0.0, None, None, None)

    lines = [line.strip() for line in text.split('\n') if line.strip()]

    # 1. Поиск цены – только в строке с ключевым словом "Цена" или знаком рубля
    price = 0.0
    price_line_idx = None
    for i, line in enumerate(lines):
        if re.search(r'\bцена\b', line, re.IGNORECASE) or '₽' in line:
            match = re.search(r'(\d[\d\s]*)', line)
            if match:
                price_str = match.group(1).replace(' ', '')
                try:
                    price = float(price_str)
                    price_line_idx = i
                    break
                except ValueError:
                    pass

    # 2. Поиск артикула (ключевое слово "Артикул")
    article = None
    article_line_idx = None
    for i, line in enumerate(lines):
        match = re.search(r'[Аа]ртикул\s*[:#]?\s*([A-Za-z0-9\-_]+)', line)
        if match:
            article = match.group(1)
            article_line_idx = i
            break

    # 3. Поиск остатка (ключевые фразы "В наличии", "Stock", "Остаток", "На складе")
    stock = None
    stock_line_idx = None
    for i, line in enumerate(lines):
        match = re.search(r'(?:В наличии|Stock|Остаток|На складе)\s*[:#]?\s*(\d+)', line, re.IGNORECASE)
        if match:
            stock = int(match.group(1))
            stock_line_idx = i
            break

    # 4. Поиск названия: строка над артикулом (если артикул найден) или первая неспециальная строка
    name = None
    if article_line_idx is not None and article_line_idx > 0:
        for i in range(article_line_idx - 1, -1, -1):
            candidate = lines[i]
            if ('фотографи' not in candidate.lower() and
                not re.search(r'\bцена\b', candidate, re.IGNORECASE) and
                'артикул' not in candidate.lower() and
                'наличи' not in candidate.lower() and
                'на складе' not in candidate.lower() and
                not re.match(r'^\d+$', candidate)):
                name = candidate
                break
        if name is None:
            name = lines[0]
    else:
        for line in lines:
            if (not re.search(r'\bцена\b', line, re.IGNORECASE) and
                'артикул' not in line.lower() and
                'наличи' not in line.lower() and
                'на складе' not in line.lower() and
                'фотографи' not in line.lower() and
                not re.match(r'^\d+$', line)):
                name = line
                break
        if name is None and lines:
            name = lines[0]

    # 5. Категория — первая неслужебная строка после цены
    category = None
    if price_line_idx is not None:
        for i in range(price_line_idx + 1, len(lines)):
            candidate = lines[i]
            if (candidate == name or
                'артикул' in candidate.lower() or
                re.search(r'\bцена\b', candidate, re.IGNORECASE) or
                'наличи' in candidate.lower() or
                'на складе' in candidate.lower() or
                'фотографи' in candidate.lower() or
                re.match(r'^\d+$', candidate)):
                continue
            category = candidate
            break

    # Удаляем хештеги из категории, если нужно
    if category:
        category = re.sub(r'#\w+', '', category).strip()

    # 6. Описание — все строки, которые не являются названием, артикулом, ценой, остатком, категорией и не содержат "фотографий"
    indices_to_skip = {price_line_idx, article_line_idx, stock_line_idx}
    if name:
        try:
            name_idx = lines.index(name)
            indices_to_skip.add(name_idx)
        except ValueError:
            pass
    if category:
        try:
            cat_idx = lines.index(category)
            indices_to_skip.add(cat_idx)
        except ValueError:
            pass
    description_lines = []
    for i, line in enumerate(lines):
        if i in indices_to_skip:
            continue
        if 'фотографи' in line.lower():
            continue
        if line == name or line == category:
            continue
        description_lines.append(line)
    description = "\n".join(description_lines) if description_lines else None

    return (name, article, price, category, description, stock)

def format_cart(order) -> str:
    """Форматирует корзину для отображения."""
    if not order.items:
        return "🛒 Корзина пуста."
    lines = [f"🛒 **Заказ #{order.id}**\n"]
    for item in order.items:
        name = escape_markdown(item.product.name) if item.product else f"Товар #{item.product_id}"
        lines.append(f"• {name}: {item.quantity} шт. × {item.price_at_order:.0f} ₽")
    lines.append(f"\n💰 **Итого: {order.total_amount:.0f} ₽**")
    return "\n".join(lines)


def format_order_for_admin(order) -> str:
    """Форматирует информацию о заказе для администратора. Приоритет: @username."""
    user = order.user
    if user:
        user_info = f"@{user.username}" if user.username else (user.full_name or "Без имени")
        phone = user.phone or "не указан"
        fio = getattr(order, "full_name", None) or (user.full_name if user else None)
    else:
        user_info = "Пользователь не найден"
        phone = "не указан"
        fio = getattr(order, "full_name", None)

    fio_line = f"🪪 ФИО: {escape_markdown(fio)}\n" if fio else ""
    address = escape_markdown(order.delivery_address) if order.delivery_address else "не указан"
    items_lines = []
    for item in order.items:
        product_name = escape_markdown(item.product.name) if item.product else f"Товар #{item.product_id}"
        items_lines.append(f"• {product_name}: {item.quantity} шт. × {item.price_at_order:.0f} ₽")
    items_text = "\n".join(items_lines)
    return (
        f"📦 **Заказ #{order.id}**\n"
        f"👤 Клиент: {user_info}\n"
        f"{fio_line}"
        f"📱 Телефон: {phone}\n"
        f"🚚 Доставка: {order.delivery_method or 'не выбран'}\n"
        f"📍 Адрес: {address}\n\n"
        f"🛒 **Товары:**\n{items_text}\n\n"
        f"💰 **Итого: {order.total_amount:.0f} ₽**"
    )

async def _upload_file_to_max(file_like: io.BytesIO, file_type: str) -> str | None:
    headers = {"Authorization": settings.max_bot_token}
    async with aiohttp.ClientSession() as session:
        # 1. Запрос URL / токена
        async with session.post(
            f"{MAX_API_BASE}/uploads?type={file_type}",
            headers=headers
        ) as resp:
            resp_text = await resp.text()
            logger.info(f"Ответ /uploads: status={resp.status}, body={resp_text}")
            if resp.status != 200:
                logger.error(f"Не удалось получить URL: {resp.status} {resp_text}")
                return None
            data = await resp.json()
            # Для видео токен часто приходит сразу в ответе /uploads
            token = data.get("token")
            if token:
                return token
            upload_url = data.get("url")
            if not upload_url:
                logger.error("Ответ /uploads не содержит URL")
                return None

        # 2. Загрузка файла
        file_like.seek(0)
        form = aiohttp.FormData()
        form.add_field("data", file_like, filename="file")
        async with session.post(upload_url, data=form) as resp:
            resp_text = await resp.text()
            logger.info(f"Загрузка файла: status={resp.status}, body={resp_text}")
            if resp.status != 200:
                logger.error(f"Ошибка загрузки: {resp.status} {resp_text}")
                return None

            # 3. Извлечение токена из ответа (универсальный подход)
            try:
                result = await resp.json()
                # Способ 1: плоский token (может быть у любого типа)
                token = result.get("token")
                if token:
                    return token
                # Способ 2: вложенный photos (характерно для фото)
                photos = result.get("photos")
                if photos:
                    for photo_id, photo_data in photos.items():
                        token = photo_data.get("token")
                        if token:
                            return token
                # Способ 3: возможно, есть другие структуры (video, file), но пока не требуется
            except Exception:
                pass

            logger.error(f"Не удалось извлечь токен из ответа: {resp_text}")
            return None

async def upload_photo_to_max(file_id: str, tg_bot: TelegramBot) -> str | None:
    """Загружает фото из Telegram в Max, возвращает Max-токен."""
    try:
        file_obj = await tg_bot.get_file(file_id)
        file_like = io.BytesIO()
        await file_obj.download_to_memory(file_like)
        file_like.seek(0)
        return await _upload_file_to_max(file_like, "image")
    except Exception as e:
        logger.warning(f"Не удалось загрузить фото {file_id} в Max: {e}")
        return None

async def upload_video_to_max(file_id: str, tg_bot: TelegramBot) -> str | None:
    """Загружает видео из Telegram в Max через aiomax, возвращает валидный токен."""
    try:
        # Скачиваем видео во временный файл
        file_obj = await tg_bot.get_file(file_id)
        file_path = f"/tmp/{file_id}.mp4"
        await file_obj.download_to_drive(file_path)

        # Создаём временный MaxBot и загружаем видео
        from aiomax import Bot as MaxBot
        import aiohttp
        max_bot = MaxBot(settings.max_bot_token)
        max_bot.session = aiohttp.ClientSession()
        max_bot.session.headers.update({'Authorization': settings.max_bot_token})

        video_attachment = await max_bot.upload_video(file_path)
        token = video_attachment.token
        logger.info(f"Видео загружено через aiomax, токен: {token}")

        await max_bot.session.close()
        return token
    except Exception as e:
        logger.warning(f"Не удалось загрузить видео {file_id} в Max: {e}")
        return None

def _parse_post_link(text: str) -> Optional[int]:
    """
    Извлекает post_id из текста: просто число или из URL /post/123.
    Также пробует извлечь из ссылки вида https://t.me/c/.../123.
    """
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    m = re.search(r'/(\d+)$', text)
    if m:
        return int(m.group(1))
    m = re.search(r'/post/(\d+)', text)
    if m:
        return int(m.group(1))
    return None