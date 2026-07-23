"""Тест Бага 5: оплаченный заказ нельзя отменить."""

from types import SimpleNamespace

import bot.handlers.checkout as checkout
from bot.db import OrderStatus


class FakeQuery:
    def __init__(self, data, user_id=1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(chat_id=1, message_id=10)
        self.edited = None
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edited = text


async def test_paid_order_cannot_be_cancelled(monkeypatch):
    order = SimpleNamespace(status=OrderStatus.paid, user_id=1, items=[],
                            total_amount=100.0, full_name=None,
                            user=SimpleNamespace(full_name=None))

    async def fake_session():
        yield object()

    async def fake_get_order(session, order_id):
        return order

    monkeypatch.setattr(checkout, "get_session", fake_session)
    monkeypatch.setattr(checkout, "get_order_with_items", fake_get_order)

    query = FakeQuery("payment:cancel:5", user_id=1)
    ctx = SimpleNamespace(bot=SimpleNamespace())
    await checkout.payment_cancel(SimpleNamespace(callback_query=query), ctx)

    # Статус не изменился, пользователю показано предупреждение.
    assert order.status == OrderStatus.paid
    assert query.edited and "оплачен" in query.edited.lower()
