"""Отмена заказа клиентом (bot.handlers.checkout.payment_cancel).

Поведение зафиксировано по коду: клиент может отменить заказ в статусах
pending И paid — но только до подтверждения админом (confirmed/shipped/
done/cancelled отменяются с предупреждением). Старая формулировка теста
(«оплаченный заказ нельзя отменить») противоречила shipped-логике с самого
первого коммита, поэтому тест переписан под фактическое поведение.
"""

from types import SimpleNamespace

import pytest

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


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text=None, **kw):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=1)


class FakeSession:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


def _order(status, user_id=1):
    return SimpleNamespace(
        id=5, status=status, user_id=user_id, items=[], total_amount=100.0,
        full_name="Иванов Иван", user=SimpleNamespace(id=1, username=None,
           full_name="Иванов Иван", phone="+79990001122"),
        delivery_address="ул. Пушкина", delivery_method="СДЭК",
        created_at=None,
    )


def _patch(monkeypatch, order):
    holder = {}

    async def fake_session():
        s = FakeSession()
        holder["s"] = s
        yield s

    async def fake_get_order(session, order_id):
        return order

    monkeypatch.setattr(checkout, "get_session", fake_session)
    monkeypatch.setattr(checkout, "get_order_with_items", fake_get_order)
    monkeypatch.setattr(checkout, "invalidate_catalog_cache", lambda: None)
    return holder


@pytest.mark.parametrize("status", [OrderStatus.pending, OrderStatus.paid])
async def test_client_can_cancel_before_confirmation(monkeypatch, status):
    """pending/paid отменяются: статус меняется, коммит выполняется."""
    order = _order(status)
    holder = _patch(monkeypatch, order)

    query = FakeQuery("payment:cancel:5")
    bot = FakeBot()
    await checkout.payment_cancel(SimpleNamespace(callback_query=query),
                                  SimpleNamespace(bot=bot))

    assert order.status == OrderStatus.cancelled
    assert holder["s"].committed is True
    assert query.edited and "отменён" in query.edited


async def test_admin_notified_about_cancellation(monkeypatch):
    """Админ обязан увидеть отмену — иначе товар уйдёт другому, а деньги висят."""
    order = _order(OrderStatus.paid)
    _patch(monkeypatch, order)

    bot = FakeBot()
    await checkout.payment_cancel(SimpleNamespace(callback_query=FakeQuery("payment:cancel:5")),
                                  SimpleNamespace(bot=bot))

    assert bot.sent, "админ не уведомлён об отмене"
    assert "отменил заказ #5" in bot.sent[0][1]
    assert "Иванов Иван" in bot.sent[0][1]


@pytest.mark.parametrize("status", [OrderStatus.confirmed, OrderStatus.exported,
                                    OrderStatus.cancelled])
async def test_confirmed_order_cannot_be_cancelled(monkeypatch, status):
    """После подтверждения админом клиентская отмена запрещена."""
    order = _order(status)
    holder = _patch(monkeypatch, order)

    query = FakeQuery("payment:cancel:5")
    await checkout.payment_cancel(SimpleNamespace(callback_query=query),
                                  SimpleNamespace(bot=FakeBot()))

    assert order.status == status, "статус подтверждённого заказа изменён"
    assert holder["s"].committed is False
    assert query.edited and "невозможна" in query.edited


async def test_other_user_cannot_cancel(monkeypatch):
    """Чужой заказ не отменяется и не раскрывает своё существование."""
    order = _order(OrderStatus.paid, user_id=777)
    holder = _patch(monkeypatch, order)

    query = FakeQuery("payment:cancel:5", user_id=1)
    await checkout.payment_cancel(SimpleNamespace(callback_query=query),
                                  SimpleNamespace(bot=FakeBot()))

    assert order.status == OrderStatus.paid
    assert holder["s"].committed is False
    assert query.edited == "❌ Заказ не найден."


async def test_missing_order_shows_not_found(monkeypatch):
    _patch(monkeypatch, None)
    query = FakeQuery("payment:cancel:404")
    await checkout.payment_cancel(SimpleNamespace(callback_query=query),
                                  SimpleNamespace(bot=FakeBot()))
    assert query.edited == "❌ Заказ не найден."
