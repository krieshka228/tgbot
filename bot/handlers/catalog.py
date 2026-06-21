"""
handlers/catalog.py — просмотр каталога покупателем.

Структура навигации (3 уровня):

    🏠 Главная → 📦 Категория → 📱 Подкатегория

* Категории и подкатегории — это ОДНО текстовое сообщение, которое
  редактируется на месте (``edit_message_text``), поэтому переходы между
  ними не плодят дубли.
* Товары показываются медиа-карточками: каждый товар — отдельное сообщение
  с фото/видео и подписью (HTML) и кнопкой «🛒 Заказать». По 5 товаров на
  страницу. После карточек идёт ОДНО навигационное сообщение с «хлебными
  крошками», счётчиком и кнопками пагинации/возврата/«🏠 Главная».
* При переключении страницы (и при выходе из товаров) все ранее
  отправленные карточки и навигационное сообщение удаляются через
  ``delete_message`` перед отправкой новых — дублей не остаётся.

Список товаров категории кэшируется на 60 секунд функцией
``get_active_products_in_category`` (bot/cache.py, ``CATALOG_CACHE_TTL``),
поэтому переключение страниц и повторные заходы не бьют по БД. Кэш
сбрасывается при любом изменении товаров (см. CLAUDE.md).

Все callback-хэндлеры обёрнуты в ``@catalog_handler`` (try/except +
``logger.exception`` + мягкий ответ пользователю), чтобы интерфейс не
«зависал» при сбое.
"""

import asyncio
import functools
import html
import logging
from collections import Counter

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, ContextTypes

from bot.config import ADMIN_USER_ID
from bot.db import (
    Product,
    get_active_products_in_category,
    get_all_active_products,
    get_bot_setting,
    get_session,
)
from bot.keyboards import kb_back_to_menu

logger = logging.getLogger(__name__)

# Товары — медиа-карточками, поэтому страница небольшая (флуд-лимиты Telegram).
PRODUCTS_PER_PAGE = 3
# Категории/подкатегории — кнопки в столбик.
LIST_PER_PAGE = 8

HOME_BUTTON = InlineKeyboardButton("🏠 Главная", callback_data="menu:main")


# ============================ ИНФРАСТРУКТУРА ============================

def catalog_handler(func):
    """Декоратор: единый try/except + ``logger.exception`` для хэндлеров.

    Любое исключение логируется с трейсбеком, а пользователю показывается
    мягкое уведомление вместо «зависшей» кнопки.
    """

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        # Хэндлеры вызываются и как (update, context), и как (query, context, page=…)
        # — пробрасываем аргументы как есть, а объект callback'а для
        # логирования/ответа определяем из первого аргумента.
        try:
            return await func(*args, **kwargs)
        except Exception:  # noqa: BLE001 — намеренно ловим всё на границе хэндлера
            obj = args[0] if args else None
            query = getattr(obj, "callback_query", None) or obj
            logger.exception(
                "catalog handler failed",
                extra={"event": "catalog_error",
                       "handler": func.__name__,
                       "callback": getattr(query, "data", None),
                       "user_id": getattr(getattr(query, "from_user", None), "id", None)},
            )
            if query is not None and hasattr(query, "answer"):
                try:
                    await query.answer("⚠️ Произошла ошибка, попробуйте позже.",
                                       show_alert=True)
                except Exception:  # noqa: BLE001
                    pass

    return wrapper


async def _safe_answer(query, text: str | None = None, *, show_alert: bool = False) -> None:
    """Отвечает на callback, не падая при повторном/просроченном ответе."""
    try:
        await query.answer(text, show_alert=show_alert)
    except Exception:  # noqa: BLE001
        pass


def _crumbs(category: str | None = None, subcategory: str | None = None) -> str:
    """«Хлебные крошки» (HTML): 🏠 Главная → 📦 Категория → 📱 Подкатегория.

    Текущий (последний) уровень выделяется жирным.
    """
    parts = ["🏠 Главная"]
    if category:
        parts.append(f"📦 {html.escape(category)}")
    if subcategory:
        parts.append(f"📱 {html.escape(subcategory)}")
    parts[-1] = f"<b>{parts[-1]}</b>"
    return " → ".join(parts)


def _pagination_row(page: int, total_pages: int, prefix: str) -> list[list]:
    """Строка пагинации ◀️/▶️ (или пустой список, если страница одна)."""
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ Назад", callback_data=f"{prefix}:{page - 1}"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("Вперёд ▶️", callback_data=f"{prefix}:{page + 1}"))
    return [row] if row else []


def _subcategory_of(product: Product) -> str:
    """Подкатегория = часть названия до первой запятой (как в исходной логике)."""
    name = product.name or ""
    return name.split(",")[0].strip() if "," in name else name.strip()


async def _render_list(query, context, text: str, keyboard: list[list]) -> None:
    """Показывает текстовый экран-список, редактируя текущее сообщение.

    Если редактирование невозможно (текущее сообщение — медиа), сообщение
    удаляется и отправляется новое — гарантируем ровно одно сообщение.
    """
    markup = InlineKeyboardMarkup(keyboard)
    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass
        await context.bot.send_message(
            chat_id=query.message.chat_id, text=text,
            reply_markup=markup, parse_mode=ParseMode.HTML,
        )


async def _safe_delete(bot, chat_id: int, message_id: int) -> None:
    """Удаляет сообщение, гася ошибки (уже удалено / нет прав / устарело)."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:  # noqa: BLE001
        pass


async def _clear_product_messages(query, context, *, keep_current: bool = True) -> None:
    """Удаляет карточки товаров и навигационное сообщение ОДНОВРЕМЕННО.

    Удаление идёт параллельно через ``asyncio.gather`` — это заметно быстрее
    последовательного при 5 карточках на странице.

    :param keep_current: не удалять ``query.message`` (его отредактируют выше
        — например, при возврате к категориям навигационное сообщение
        превращается в список категорий редактированием на месте).
    """
    bot = context.bot
    chat_id = query.message.chat_id
    ids = list(context.user_data.pop("catalog_product_msgs", []))
    nav = context.user_data.pop("catalog_nav_msg_id", None)
    if nav is not None:
        ids.append(nav)
    to_delete = [mid for mid in ids
                 if not (keep_current and mid == query.message.message_id)]
    if to_delete:
        await asyncio.gather(*[_safe_delete(bot, chat_id, mid) for mid in to_delete])


# ========================= УРОВЕНЬ 1: КАТЕГОРИИ =========================

@catalog_handler
async def catalog_show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  page: int = 0):
    """Список категорий с количеством активных товаров в каждой."""
    query = update.callback_query
    await _safe_answer(query)
    # Если пришли из товаров — убираем карточки, текущее сообщение редактируем.
    await _clear_product_messages(query, context, keep_current=True)
    context.user_data.pop("catalog_current_sub", None)

    async for session in get_session():
        products = await get_all_active_products(session)

    counts = Counter(p.category for p in products if p.category)
    if not counts:
        await _render_list(query, context, "📭 В каталоге пока нет товаров.",
                           [[HOME_BUTTON]])
        return

    categories = sorted(counts)
    total_pages = (len(categories) - 1) // LIST_PER_PAGE + 1
    page = max(0, min(page, total_pages - 1))
    page_cats = categories[page * LIST_PER_PAGE: page * LIST_PER_PAGE + LIST_PER_PAGE]

    # Маппинг индекс→категория (в callback_data нельзя класть произвольный текст).
    context.user_data["catalog_cats"] = dict(enumerate(page_cats))

    keyboard = [
        [InlineKeyboardButton(f"📦 {cat} ({counts[cat]})", callback_data=f"catalog:sc:{idx}")]
        for idx, cat in enumerate(page_cats)
    ]
    keyboard += _pagination_row(page, total_pages, "catalog:catlist")
    keyboard.append([HOME_BUTTON])

    header = f"{_crumbs()}\n📂 <b>Категории</b> · стр. {page + 1}/{total_pages}"
    logger.info("catalog: categories", extra={"event": "catalog_categories",
                                               "user_id": query.from_user.id, "page": page})
    await _render_list(query, context, header, keyboard)


# ======================= УРОВЕНЬ 2: ПОДКАТЕГОРИИ ========================

@catalog_handler
async def catalog_show_subcategories(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     page: int = 0):
    """Подкатегории выбранной категории с количеством товаров."""
    query = update.callback_query
    await _safe_answer(query)
    await _clear_product_messages(query, context, keep_current=True)

    category = context.user_data.get("catalog_current_cat")
    if not category:
        await catalog_show_categories(update, context)
        return

    async for session in get_session():
        products = await get_active_products_in_category(session, category)

    counts = Counter(_subcategory_of(p) for p in products)
    if not counts:
        await _render_list(
            query, context,
            f"{_crumbs(category)}\n\nВ этой категории пока нет товаров 😔",
            [[InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")],
             [HOME_BUTTON]],
        )
        return

    subs = sorted(counts)
    total_pages = (len(subs) - 1) // LIST_PER_PAGE + 1
    page = max(0, min(page, total_pages - 1))
    page_subs = subs[page * LIST_PER_PAGE: page * LIST_PER_PAGE + LIST_PER_PAGE]

    context.user_data["catalog_subs"] = dict(enumerate(page_subs))

    keyboard = [
        [InlineKeyboardButton(f"📱 {sub} ({counts[sub]})", callback_data=f"catalog:ss:{idx}")]
        for idx, sub in enumerate(page_subs)
    ]
    keyboard += _pagination_row(page, total_pages, "catalog:sublist")
    keyboard.append([InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")])
    keyboard.append([HOME_BUTTON])

    header = f"{_crumbs(category)}\nВыберите подкатегорию (стр. {page + 1}/{total_pages}):"
    logger.info("catalog: subcategories", extra={"event": "catalog_subcategories",
                                                  "user_id": query.from_user.id,
                                                  "category": category, "page": page})
    await _render_list(query, context, header, keyboard)


# =================== УРОВЕНЬ 3: ТОВАРЫ (МЕДИА-КАРТОЧКИ) ==================

def _product_caption(product: Product) -> str:
    """HTML-подпись карточки: жирный заголовок, разделители ▫️, цена, описание."""
    lines = [f"<b>{html.escape(product.name or 'Без названия')}</b>"]
    if product.article:
        lines.append(f"▫️ Артикул: <code>{html.escape(product.article)}</code>")
    if product.stock is not None:
        lines.append(f"▫️ На складе: {product.stock} шт.")
    lines.append(f"▫️ Цена: <b>{product.price:.0f} ₽</b>")
    if product.description:
        lines.append("")
        lines.append(html.escape(product.description))
    return "\n".join(lines)


async def _send_product_card(bot, chat_id: int, product: Product) -> list[int]:
    caption = _product_caption(product)  # HTML-подпись
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛒 Заказать", callback_data=f"order:start:{product.id}")]]
    )

    photos = [p for p in product.photo_file_ids.split(",") if p] if product.photo_file_ids else []
    videos = [v for v in product.video_file_ids.split(",") if v] if product.video_file_ids else []

    media: list[InputMediaPhoto | InputMediaVideo] = []
    for photo_id in photos:
        media.append(InputMediaPhoto(media=photo_id))
    for video_id in videos:
        media.append(InputMediaVideo(media=video_id))

    # Случай 1: нет медиа вообще — обычное текстовое сообщение с кнопкой
    if not media:
        m = await bot.send_message(
            chat_id, text=caption, reply_markup=keyboard, parse_mode=ParseMode.HTML
        )
        return [m.message_id]

    # Случай 2: ровно один файл — caption и кнопка в одном сообщении, без альбома
    if len(media) == 1:
        if photos:
            m = await bot.send_photo(
                chat_id, photo=photos[0], caption=caption,
                reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
        else:
            m = await bot.send_video(
                chat_id, video=videos[0], caption=caption,
                reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
        return [m.message_id]

    # Случай 3: несколько файлов — отправляем альбомом,
    # подпись вешаем на первый элемент, кнопку — отдельным сообщением сразу после.
    first = media[0]
    if isinstance(first, InputMediaPhoto):
        media[0] = InputMediaPhoto(media=first.media, caption=caption, parse_mode=ParseMode.HTML)
    else:
        media[0] = InputMediaVideo(media=first.media, caption=caption, parse_mode=ParseMode.HTML)

    messages = await bot.send_media_group(chat_id, media=media)
    message_ids = [msg.message_id for msg in messages]

    # Telegram не позволяет прикрепить inline-клавиатуру к сообщению внутри media group
    # через send_media_group, а edit_message_reply_markup ненадёжен из-за гонок/ретраев.
    # Поэтому кнопку шлём отдельным служебным сообщением — это гарантированно работает.
    btn_msg = await bot.send_message(
        chat_id,
        text="\u2063",  # invisible separator, чтобы не плодить лишний видимый текст
        reply_markup=keyboard,
    )
    message_ids.append(btn_msg.message_id)

    return message_ids

@catalog_handler
async def show_products_page(query, context, page: int = 0):
    """Показывает страницу товаров медиа-карточками (3/стр.) + навигацию."""
    await _safe_answer(query, "⏳ Загрузка...")
    # Возврат к списку отменяет незавершённый ввод количества (state order_qty).
    context.user_data.pop("state", None)
    chat_id = query.message.chat_id
    bot = context.bot

    category = context.user_data.get("catalog_current_cat")
    subcategory = context.user_data.get("catalog_current_sub")

    back_btn = (
        InlineKeyboardButton("↩️ К подкатегориям", callback_data="catalog:back_to_subs")
        if subcategory else
        InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")
    )

    # Чистим прошлые карточки/навигацию и сообщение, с которого пришли (список
    # подкатегорий или старую навигацию) — чтобы новые карточки шли «с чистого листа».
    await _clear_product_messages(query, context, keep_current=False)
    try:
        await query.message.delete()
    except Exception:  # noqa: BLE001
        pass

    if not category:
        msg = await bot.send_message(
            chat_id, "Сначала выберите категорию.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩️ К категориям", callback_data="catalog:show")]]),
        )
        context.user_data["catalog_nav_msg_id"] = msg.message_id
        return

    async for session in get_session():
        all_products = await get_active_products_in_category(session, category)

    if subcategory:
        products_all = [p for p in all_products
                        if p.name == subcategory or p.name.startswith(subcategory + ",")]
    else:
        products_all = all_products

    # Пустая категория/подкатегория — одно сообщение с кнопкой «Назад».
    if not products_all:
        msg = await bot.send_message(
            chat_id,
            f"{_crumbs(category, subcategory)}\n\nВ этой категории пока нет товаров 😔",
            reply_markup=InlineKeyboardMarkup([[back_btn], [HOME_BUTTON]]),
            parse_mode=ParseMode.HTML,
        )
        context.user_data["catalog_nav_msg_id"] = msg.message_id
        logger.info("catalog: empty products", extra={"event": "catalog_products_empty",
                                                       "user_id": query.from_user.id,
                                                       "category": category,
                                                       "subcategory": subcategory})
        return

    total = len(products_all)
    total_pages = (total - 1) // PRODUCTS_PER_PAGE + 1
    page = max(0, min(page, total_pages - 1))
    context.user_data["catalog_prod_page"] = page
    page_products = products_all[page * PRODUCTS_PER_PAGE:
                                 page * PRODUCTS_PER_PAGE + PRODUCTS_PER_PAGE]

    # 1) Карточки товаров отправляются ПАРАЛЛЕЛЬНО (asyncio.gather) с небольшим
    # сдвигом старта (~0.06 с между запусками), чтобы перекрыть сетевые задержки
    # и при этом не упереться во флуд-лимиты Telegram. gather сохраняет порядок
    # результатов = порядок товаров, поэтому id карточек собираются по порядку.
    new_msgs = []
    for i, p in enumerate(page_products):
        ids = await _send_product_card(bot, chat_id, p)
        new_msgs.extend(ids)
        # Небольшая задержка между карточками (опционально, чтобы не упереться в лимиты)
        await asyncio.sleep(0.1)

    # 2) Навигационное сообщение под карточками: крошки, счётчик, пагинация.
    nav_text = (f"{_crumbs(category, subcategory)}\n"
                f"🔎 Найдено {total} товаров. Страница {page + 1} из {total_pages}")
    nav_kb = _pagination_row(page, total_pages, "catalog:prodpage")
    nav_kb.append([back_btn])
    nav_kb.append([HOME_BUTTON])
    nav_msg = await bot.send_message(chat_id, nav_text,
                                     reply_markup=InlineKeyboardMarkup(nav_kb),
                                     parse_mode=ParseMode.HTML)
    context.user_data["catalog_nav_msg_id"] = nav_msg.message_id

    logger.info("catalog: products page", extra={"event": "catalog_products",
                                                 "user_id": query.from_user.id,
                                                 "category": category,
                                                 "subcategory": subcategory,
                                                 "page": page, "total": total})

    # 3) Префетч следующей страницы в фоне: прогреваем 60-секундный TTL-кэш
    # списка товаров категории, чтобы переход «Вперёд ▶️» был мгновенным.
    # (Весь список категории кэшируется одним вызовом, поэтому это и есть
    # данные следующей страницы.)
    if page + 1 < total_pages:
        asyncio.create_task(_prefetch_category(category))


async def _prefetch_category(category: str) -> None:
    """Фоновый прогрев кэша списка товаров категории (для следующей страницы)."""
    try:
        async for session in get_session():
            await get_active_products_in_category(session, category)
    except Exception:  # noqa: BLE001 — фоновая задача не должна влиять на UX
        logger.debug("prefetch failed", extra={"category": category})


# ============================== ЗАКАЗ ==================================

@catalog_handler
async def start_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Кнопка «Заказать» из карточки — запрашивает количество."""
    query = update.callback_query
    await _safe_answer(query)

    # Доступность реквизитов (QR) — без них оформление невозможно.
    async for session in get_session():
        token = await get_bot_setting(session, "payment_qr_token")
        if not token:
            if query.from_user.id == ADMIN_USER_ID:
                await _safe_answer(query, "⚠️ QR-код не задан. Загрузите его в админ‑меню.",
                                   show_alert=True)
            else:
                await _safe_answer(query, "⚠️ Бот временно недоступен.", show_alert=True)
            return
        break

    product_id = int(query.data.split(":")[-1])
    async for session in get_session():
        product = await session.get(Product, product_id)
        break
    if not product or not product.is_active:
        await _safe_answer(query, "Товар недоступен.", show_alert=True)
        return

    # Закрываем все карточки текущей страницы + навигацию.
    chat_id = query.message.chat_id
    await _clear_product_messages(query, context, keep_current=False)
    try:
        await query.message.delete()
    except Exception:  # noqa: BLE001
        pass

    text = _product_caption(product) + "\n\n✏️ Введите количество:"
    photos = product.photo_file_ids.split(",") if product.photo_file_ids else []
    videos = product.video_file_ids.split(",") if product.video_file_ids else []

    # Кнопка «Назад к списку» возвращает на текущую страницу каталога (Баг 3).
    back_page = context.user_data.get("catalog_prod_page", 0)
    order_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ К списку", callback_data=f"catalog:prodpage:{back_page}")],
        [HOME_BUTTON],
    ])

    card_msg_id = None
    try:
        if photos:
            m = await context.bot.send_photo(chat_id, photo=photos[0], caption=text,
                                             reply_markup=order_kb, parse_mode=ParseMode.HTML)
            card_msg_id = m.message_id
        elif videos:
            m = await context.bot.send_video(chat_id, video=videos[0], caption=text,
                                             reply_markup=order_kb, parse_mode=ParseMode.HTML)
            card_msg_id = m.message_id
        else:
            m = await context.bot.send_message(chat_id, text=text,
                                               reply_markup=order_kb, parse_mode=ParseMode.HTML)
            card_msg_id = m.message_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("order prompt media failed",
                       extra={"event": "media_error", "product_id": product.id,
                              "error": repr(exc)})
        m = await context.bot.send_message(chat_id, text=text,
                                           reply_markup=order_kb, parse_mode=ParseMode.HTML)
        card_msg_id = m.message_id

    context.user_data["state"] = "order_qty"
    context.user_data["data"] = {"product_id": product_id, "card_msg_id": card_msg_id}
    logger.info("catalog: order started", extra={"event": "order_start",
                                                 "user_id": query.from_user.id,
                                                 "product_id": product.id})


# ========================== ПОИСК (входы) =============================

@catalog_handler
async def search_name_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["state"] = "search_name"
    await query.edit_message_text("🔎 Введите название товара (или его часть):",
                                  reply_markup=kb_back_to_menu())


@catalog_handler
async def search_article_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data["state"] = "search_article"
    await query.edit_message_text("🔎 Введите артикул:", reply_markup=kb_back_to_menu())


# ========================== РЕГИСТРАЦИЯ ===============================

def _page_arg(update: Update) -> int:
    """Достаёт номер страницы из callback_data вида '...:<page>'."""
    return int(update.callback_query.data.split(":")[-1])


@catalog_handler
async def _select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    idx = int(query.data.split(":")[2])
    category = context.user_data.get("catalog_cats", {}).get(idx)
    if not category:
        await _safe_answer(query, "Категория не найдена, обновите каталог.", show_alert=True)
        await catalog_show_categories(update, context)
        return
    context.user_data["catalog_current_cat"] = category
    context.user_data.pop("catalog_current_sub", None)
    await catalog_show_subcategories(update, context, page=0)


@catalog_handler
async def _select_subcategory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    idx = int(query.data.split(":")[2])
    subcategory = context.user_data.get("catalog_subs", {}).get(idx)
    if not subcategory:
        await _safe_answer(query, "Подкатегория не найдена, обновите каталог.", show_alert=True)
        await catalog_show_subcategories(update, context)
        return
    context.user_data["catalog_current_sub"] = subcategory
    await show_products_page(query, context, page=0)


@catalog_handler
async def _back_to_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    context.user_data.pop("catalog_current_sub", None)
    await catalog_show_subcategories(update, context, page=0)


@catalog_handler
async def _prodpage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await _safe_answer(query)
    await show_products_page(query, context, page=int(query.data.split(":")[2]))


def register(app):
    # Категории (уровень 1) и пагинация.
    app.add_handler(CallbackQueryHandler(catalog_show_categories, pattern="^catalog:show$"))
    app.add_handler(CallbackQueryHandler(
        lambda u, c: catalog_show_categories(u, c, page=_page_arg(u)),
        pattern="^catalog:catlist:",
    ))
    # Подкатегории (уровень 2) и пагинация.
    app.add_handler(CallbackQueryHandler(
        lambda u, c: catalog_show_subcategories(u, c, page=_page_arg(u)),
        pattern="^catalog:sublist:",
    ))
    app.add_handler(CallbackQueryHandler(_select_category, pattern="^catalog:sc:"))
    app.add_handler(CallbackQueryHandler(_select_subcategory, pattern="^catalog:ss:"))

    # Товары (уровень 3): возврат к подкатегориям и пагинация карточек.
    app.add_handler(CallbackQueryHandler(_back_to_subs, pattern="^catalog:back_to_subs$"))
    app.add_handler(CallbackQueryHandler(_prodpage, pattern="^catalog:prodpage:"))

    # Заказ.
    app.add_handler(CallbackQueryHandler(start_order, pattern="^order:start:"))

    # Поиск.
    app.add_handler(CallbackQueryHandler(search_name_start, pattern="^search:name$"))
    app.add_handler(CallbackQueryHandler(search_article_start, pattern="^search:article$"))
