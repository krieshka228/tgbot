"""Тесты error_handler: троттлинг уведомлений и обработка Conflict/Forbidden.

Регрессия: telegram.error.Conflict при поллинге не входит в _TRANSIENT
(Conflict наследуется от TelegramError, а не от NetworkError), поэтому каждая
ошибка уходила администратору полным трейсбеком — при интервале ~8 с это
сотни сообщений в Telegram. Теперь уведомление приходит не чаще раза в час.
"""

from types import SimpleNamespace

import pytest

import bot.error_handler as eh
from telegram.error import Conflict, Forbidden, NetworkError


def _RealUpdate(update_id: int = 1):
    """Настоящий telegram.Update — чтобы isinstance(update, Update) сработал."""
    from telegram import Message, Update

    return Update(
        update_id=update_id,
        message=Message(
            message_id=1,
            date=None,
            chat=SimpleNamespace(id=1, type="private"),
        ),
    )


class _FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return True


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Сбрасываем троттлинг и гарантируем наличие ADMIN_CHAT_ID."""
    eh._last_notified.clear()
    eh._suppressed.clear()
    monkeypatch.setattr(eh, "ADMIN_CHAT_ID", 777, raising=True)
    yield
    eh._last_notified.clear()
    eh._suppressed.clear()


def _ctx(exc):
    bot = _FakeBot()
    return bot, SimpleNamespace(error=exc, bot=bot)


async def test_conflict_notifies_admin_once_per_hour():
    exc = Conflict("terminated by other getUpdates request")
    bot1, ctx1 = _ctx(exc)
    await eh.error_handler(None, ctx1)
    assert len(bot1.sent) == 1, "первый Conflict должен отправить уведомление"
    assert "Conflict" in bot1.sent[0][1]

    # Повторы в том же окне — уведомлений больше нет, но логирование продолжается.
    for _ in range(50):
        bot, ctx = _ctx(Conflict("terminated by other getUpdates request"))
        await eh.error_handler(None, ctx)
        assert bot.sent == []


async def test_conflict_second_notification_after_cooldown(monkeypatch):
    fake = {"t": 1000.0}
    monkeypatch.setattr(eh, "_now", lambda: fake["t"])

    bot0, ctx0 = _ctx(Conflict("conflict A"))
    await eh.error_handler(None, ctx0)
    assert len(bot0.sent) == 1

    # Внутри окна троттлинга — молчим.
    fake["t"] += 60.0
    bot1, ctx1 = _ctx(Conflict("conflict A2"))
    await eh.error_handler(None, ctx1)
    assert bot1.sent == []

    # Сдвигаем время за пределы окна — уведомление уходит снова.
    fake["t"] += eh._NOTIFY_COOLDOWN_SEC
    bot2, ctx2 = _ctx(Conflict("conflict B"))
    await eh.error_handler(None, ctx2)
    assert len(bot2.sent) == 1, "после истечения часа уведомление должно уйти снова"


async def test_conflict_suppressed_count_reported():
    for _ in range(3):
        await eh.error_handler(None, _ctx(Conflict("x"))[1])
    assert eh._suppressed.get("conflict") == 2


async def test_conflict_does_not_answer_update():
    """Conflict — не ошибка пользователя: отвечать на апдейт нельзя.

    Проверяем, что ветка Conflict возвращается до обработки update: апдейт
    остаётся неотреагированным (PTB сам переподключится).
    """
    update = _RealUpdate(update_id=1)
    bot, ctx = _ctx(Conflict("x"))
    await eh.error_handler(update, ctx)
    assert bot.sent, "админ должен получить уведомление"
    # Ответ пользователю не отправлялся: единственный send_message — админу.
    assert len(bot.sent) == 1
    assert bot.sent[0][0] == 777


async def test_forbidden_is_quiet():
    bot, ctx = _ctx(Forbidden("bot was blocked by the user"))
    await eh.error_handler(None, ctx)
    assert bot.sent == [], "блокировка бота пользователем — не повод дёргать админа"


async def test_transient_stays_quiet():
    bot, ctx = _ctx(NetworkError("timed out"))
    await eh.error_handler(None, ctx)
    assert bot.sent == []


async def test_generic_exception_still_notifies():
    bot, ctx = _ctx(ValueError("boom"))
    await eh.error_handler(None, ctx)
    assert len(bot.sent) == 1
    assert "boom" in bot.sent[0][1]


async def test_generic_exception_throttled_per_type():
    await eh.error_handler(None, _ctx(ValueError("a"))[1])
    bot, ctx = _ctx(ValueError("b"))
    await eh.error_handler(None, ctx)
    assert bot.sent == [], "повтор того же типа ошибки не должен спамить"


async def test_no_admin_chat_no_crash(monkeypatch):
    monkeypatch.setattr(eh, "ADMIN_CHAT_ID", 0)
    await eh.error_handler(None, _ctx(Conflict("x"))[1])
    await eh.error_handler(None, _ctx(ValueError("y"))[1])
