from telegram import InlineKeyboardMarkup, InlineKeyboardButton


from telegram import ReplyKeyboardMarkup, KeyboardButton

def reply_main_menu():
    """Постоянная клавиатура с кнопкой 'Главное меню'."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🏠 Главное меню")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def kb_consent():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Даю согласие", callback_data="consent:yes")],
        [InlineKeyboardButton("❌ Нет", callback_data="consent:no")]
    ])

def kb_main_menu(is_admin: bool = False):
    buttons = [
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog:show")],
        [InlineKeyboardButton("🛒 Корзина", callback_data="cart:view")],
        [InlineKeyboardButton("📋 Мои заказы", callback_data="orders:list")],
        [InlineKeyboardButton("✉️ Написать администратору", callback_data="contact:admin")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton("⚙️ Админ-меню", callback_data="admin:menu")])
    return InlineKeyboardMarkup(buttons)

def kb_back_to_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])

def kb_admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Отчёт за месяц", callback_data="admin:excel:summary")],
        [InlineKeyboardButton("👥 База клиентов", callback_data="admin:excel:clients")],
        [InlineKeyboardButton("🔄 Синхронизация товаров", callback_data="admin:sync")],
        [InlineKeyboardButton("💳 Реквизиты", callback_data="admin:payment_qr")],
        [InlineKeyboardButton("📦 Управление остатками", callback_data="admin:set_stock_list")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])
def kb_cart_actions(order_id: int, has_items: bool = True):
    kb = []
    if has_items:
        kb.append([
            InlineKeyboardButton("✏️ Изменить кол-во", callback_data=f"cart:edit:{order_id}"),
            InlineKeyboardButton("🗑 Удалить позицию", callback_data=f"cart:remove:{order_id}")
        ])
        kb.append([InlineKeyboardButton("✅ Оформить заказ", callback_data=f"cart:checkout:{order_id}")])
    kb.append([InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(kb)

def kb_cart_items_remove(order):
    buttons = []
    for item in order.items:
        name = item.product.name if item.product else f"Товар #{item.product_id}"
        buttons.append([InlineKeyboardButton(f"❌ {name} (x{item.quantity})", callback_data=f"cart:del_item:{item.id}")])
    buttons.append([InlineKeyboardButton("↩️ Назад", callback_data="cart:view")])
    return InlineKeyboardMarkup(buttons)

def kb_payment(order_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Я оплатил — отправить чек", callback_data=f"payment:receipt:{order_id}")],
        [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"payment:cancel:{order_id}")],
        [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]
    ])

def kb_admin_confirm_payment(order_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"admin:pay_ok:{order_id}")],
        [InlineKeyboardButton("❌ Отклонить оплату", callback_data=f"admin:pay_fail:{order_id}")]
    ])
def kb_admin_sync():
    """Клавиатура с кнопкой завершения ручной синхронизации."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔚 Завершить синхронизацию", callback_data="admin:sync:finish")]
    ])