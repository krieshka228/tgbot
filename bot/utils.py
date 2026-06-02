"""
utils.py — Вспомогательные функции для парсинга постов, форматирования корзины и т.д.
"""

import re
from typing import Optional


def parse_quantity(text: str) -> Optional[int]:
    """Извлекает целое количество из текста (например, '3 шт', '5')."""
    if not text:
        return None
    match = re.search(r'\d+', text)
    return int(match.group()) if match else None


def parse_post_product(text: str) -> tuple:
    """
    Парсит пост канала с товаром.
    Возвращает кортеж: (название, артикул, цена, категория, описание, остаток)
    Если распарсить не удалось, возвращает (None, None, 0.0, None, None, None).
    """
    if not text:
        return (None, None, 0.0, None, None, None)

    lines = [line.strip() for line in text.split('\n') if line.strip()]

    # 1. Поиск цены – ТОЛЬКО в строке с ключевым словом "Цена" или знаком рубля
    price = 0.0
    price_line_idx = None
    for i, line in enumerate(lines):
        # Ищем "цена" (как отдельное слово) или символ ₽
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

    # 3. Поиск остатка (ключевые фразы "В наличии", "Stock", "Остаток")
    stock = None
    stock_line_idx = None
    for i, line in enumerate(lines):
        match = re.search(r'(?:В наличии|Stock|Остаток)\s*[:#]?\s*(\d+)', line, re.IGNORECASE)
        if match:
            stock = int(match.group(1))
            stock_line_idx = i
            break

    # 4. Поиск названия: строка над артикулом (если артикул найден) или первая неспециальная строка
    name = None
    if article_line_idx is not None and article_line_idx > 0:
        # Строка перед артикулом, не содержащая служебных слов (фото, цена, остаток)
        for i in range(article_line_idx - 1, -1, -1):
            candidate = lines[i]
            if ('фотографи' not in candidate.lower() and
                not re.search(r'\bцена\b', candidate, re.IGNORECASE) and
                'артикул' not in candidate.lower() and
                'наличи' not in candidate.lower() and
                not re.match(r'^\d+$', candidate)):  # не число
                name = candidate
                break
        if name is None:
            name = lines[0]  # fallback
    else:
        # Если артикула нет, берём первую строку, не похожую на цену/остаток
        for line in lines:
            if (not re.search(r'\bцена\b', line, re.IGNORECASE) and
                'артикул' not in line.lower() and
                'наличи' not in line.lower() and
                'фотографи' not in line.lower() and
                not re.match(r'^\d+$', line)):
                name = line
                break
        if name is None and lines:
            name = lines[0]

    # 5. Категория — первое слово названия (до запятой или пробела)
    category = None
    if name:
        parts = name.split(',')
        first_part = parts[0].strip()
        words = first_part.split()
        if words:
            category = words[0]

    # 6. Описание — все строки, которые не являются названием, артикулом, ценой, остатком и не содержат "фотографий"
    indices_to_skip = {price_line_idx, article_line_idx, stock_line_idx}
    if name:
        try:
            name_idx = lines.index(name)
            indices_to_skip.add(name_idx)
        except ValueError:
            pass
    description_lines = []
    for i, line in enumerate(lines):
        if i in indices_to_skip:
            continue
        if 'фотографи' in line.lower():
            continue
        if line == name:
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
        name = item.product.name if item.product else f"Товар #{item.product_id}"
        lines.append(f"• {name}: {item.quantity} шт. × {item.price_at_order:.0f} ₽")
    lines.append(f"\n💰 **Итого: {order.total_amount:.0f} ₽**")
    return "\n".join(lines)


def format_order_for_admin(order) -> str:
    """Форматирует информацию о заказе для администратора."""
    user = order.user
    user_info = f"{user.full_name or 'Без имени'} (ID {user.id})"
    items_lines = []
    for item in order.items:
        product_name = item.product.name if item.product else f"Товар #{item.product_id}"
        items_lines.append(f"• {product_name}: {item.quantity} шт. × {item.price_at_order:.0f} ₽")
    items_text = "\n".join(items_lines)
    return (
        f"📦 **Заказ #{order.id}**\n"
        f"👤 Клиент: {user_info}\n"
        f"📱 Телефон: {user.phone or 'не указан'}\n"
        f"🚚 Доставка: {order.delivery_method or 'не выбран'}\n"
        f"📍 Адрес: {order.delivery_address or 'не указан'}\n\n"
        f"🛒 **Товары:**\n{items_text}\n\n"
        f"💰 **Итого: {order.total_amount:.0f} ₽**"
    )


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