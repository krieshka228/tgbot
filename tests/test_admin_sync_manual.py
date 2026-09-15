"""Регрессии ручной синхронизации товаров (admin:sync).

Путь: ``process_admin_sync`` + отложенная обработка альбомов
``_process_delayed_media_group``. Именно здесь товары добавляет админ,
пересылая посты из канала. Найденные и исправленные баги:

1. Альбом обрабатывается через ``asyncio.create_task`` ВНЕ цепочки PTB,
   поэтому исключение не попадало в ``error_handler``: товар молча
   терялся (админ видел только удалённое сообщение).
2. ``stock`` отбрасывался в ``_stock``, а пометка «продано» не учитывалась —
   товар с «На складе: 5» создавался с нулевым остатком, а проданный
   публиковался как доступный.
3. Загрузка видео в Max была закомментирована, поэтому товары из ручного
   пути сохранялись без ``max_video_ids`` (в автосинхронизации канала —
   с ними).
4. ``last_sync_msg_id`` только читался, но никогда не записывался —
   промежуточные отчёты «✅ Добавлен товар» не удалялись и копились стеной.

Тесты работают без сети и без БД: сессии и апсерт подменяются фейками.
"""

import ast
import inspect
import types
from unittest.mock import AsyncMock, patch

import pytest

import bot.handlers.fsm_inputs as fi


ADMIN_ID = 7943047063


# ------------------------------ фейки ------------------------------

class FakeBot:
    """Мини-бот: запоминает отправленные сообщения и удаления."""

    def __init__(self):
        self.sent = []        # (chat_id, text)
        self.deleted = []     # (chat_id, message_id)
        self._mid = 5000

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))
        self._mid += 1
        return types.SimpleNamespace(message_id=self._mid, chat_id=chat_id)

    async def delete_message(self, chat_id, message_id, **kw):
        self.deleted.append((chat_id, message_id))
        return True


class FakeMessage:
    def __init__(self, text, chat_id=1, message_id=100, user_id=ADMIN_ID):
        self.text = text
        self.caption = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.from_user = types.SimpleNamespace(id=user_id)
        self.photo = None
        self.video = None
        self.forward_origin = None
        self.media_group_id = None
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakeSession:
    """Пустая сессия: select ничего не находит (товар не дубликат)."""

    async def execute(self, *a, **kw):
        res = types.SimpleNamespace()
        res.scalars = lambda: types.SimpleNamespace(all=lambda: [])
        return res


async def fake_get_session():
    yield FakeSession()


TXT_STOCK = "Агат, форма шар, 6 мм\nАртикул 1111\nЦена 500\nНа складе: 5\n\nКамни"
TXT_SOLD = "Яшма, форма овал, 6 мм\nАртикул 2222\nЦена 300\nпродано\n\nКамни"
TXT_PLAIN = "Гематит, форма шар, 4 мм\nАртикул 9999\nЦена 190\n\nКамни"


@pytest.fixture
def patched_sync(monkeypatch):
    """Перехватывает БД/загрузку медиа, оставляя логику process_admin_sync."""
    monkeypatch.setattr(fi, "get_session", fake_get_session)
    product = types.SimpleNamespace(
        id=1, name="Агат, форма шар, 6 мм", stock=5, is_active=True,
        max_photo_ids=None, photo_file_ids=None,
        max_video_ids=None, video_file_ids=None,
    )
    upsert = AsyncMock(return_value=product)
    monkeypatch.setattr(fi, "upsert_product", upsert)
    monkeypatch.setattr(fi, "upload_photo_to_max", AsyncMock(return_value="PHOTOTOK"))
    monkeypatch.setattr(fi, "upload_video_to_max", AsyncMock(return_value="VIDEOTOK"))
    return upsert


def _ctx(bot, **user_data):
    return types.SimpleNamespace(bot=bot, user_data={"state": "admin_sync", **user_data})


# --------------------- 1. молчаливая потеря товара ---------------------

async def test_album_failure_notifies_admin_and_keeps_messages(monkeypatch):
    """Сбой в фоновой задаче альбома обязан сообщить админу.

    Без этого потеря товара неотличима от успешной синхронизации: сообщение
    админа удалялось, а исключение оставалось только в «Task exception was
    never retrieved».
    """
    monkeypatch.setattr(fi, "MEDIA_GROUP_TIMEOUT", 0)
    monkeypatch.setattr(
        fi, "process_admin_sync",
        AsyncMock(side_effect=RuntimeError("сбой Telegram API")),
    )

    bot = FakeBot()
    ctx = _ctx(bot)
    ctx.user_data["media_buffer"] = {"g1": {
        "caption": TXT_PLAIN,
        "photos": ["P1", "P2"],
        "videos": [],
        "messages": [11, 12],
        "chat_id": 7,
        "user_id": ADMIN_ID,
    }}

    # Ровно так это делает роутер состояний: create_task без обёртки.
    task = fi.asyncio.create_task(fi._process_delayed_media_group(ctx, "g1"))
    await task

    assert task.done()
    assert task.exception() is None, "исключение не должно утекать из фоновой задачи"
    assert len(bot.sent) == 1, "админ обязан получить уведомление о сбое"
    assert bot.sent[0][0] == 7
    assert "Не удалось добавить товар" in bot.sent[0][1]
    assert "сбой Telegram API" in bot.sent[0][1]
    # Исходные сообщения НЕ удаляем — их можно переслать повторно.
    assert bot.deleted == [], "при сбое сообщения альбома удалять нельзя"
    assert ctx.user_data["media_buffer"] == {}, "буфер должен быть очищен"


async def test_album_success_deletes_source_messages(monkeypatch):
    """При успехе исходные сообщения альбома удаляются (прежнее поведение)."""
    monkeypatch.setattr(fi, "MEDIA_GROUP_TIMEOUT", 0)
    called = {}

    async def ok_sync(message, text, context, photos=None, videos=None):
        called["photos"] = photos
        called["videos"] = videos
        return True

    monkeypatch.setattr(fi, "process_admin_sync", ok_sync)

    bot = FakeBot()
    ctx = _ctx(bot)
    ctx.user_data["media_buffer"] = {"g1": {
        "caption": TXT_PLAIN, "photos": ["P1", "P2"], "videos": ["V1"],
        "messages": [11, 12], "chat_id": 7, "user_id": ADMIN_ID,
    }}
    await fi._process_delayed_media_group(ctx, "g1")

    assert sorted(bot.deleted) == [(7, 11), (7, 12)]
    assert called["photos"] == ["P1", "P2"]
    assert called["videos"] == ["V1"]


async def test_notify_failure_does_not_mask_original_error(monkeypatch):
    """Если не удалось даже отправить уведомление — задача не падает наружу."""
    monkeypatch.setattr(fi, "MEDIA_GROUP_TIMEOUT", 0)
    monkeypatch.setattr(
        fi, "process_admin_sync", AsyncMock(side_effect=RuntimeError("сбой")),
    )
    bot = FakeBot()
    bot.send_message = AsyncMock(side_effect=RuntimeError("Telegram недоступен"))
    ctx = _ctx(bot)
    ctx.user_data["media_buffer"] = {"g1": {
        "caption": TXT_PLAIN, "photos": [], "videos": [],
        "messages": [11], "chat_id": 7, "user_id": ADMIN_ID,
    }}
    task = fi.asyncio.create_task(fi._process_delayed_media_group(ctx, "g1"))
    await task
    assert task.exception() is None


# ------------------- 2. stock и «продано» в ручном пути -------------------

async def test_manual_sync_keeps_stock_from_text(patched_sync):
    """«На складе: 5» обязан доехать до БД, а не превратиться в 0."""
    bot = FakeBot()
    ctx = _ctx(bot)
    await fi.process_admin_sync(FakeMessage(TXT_STOCK), TXT_STOCK, ctx)

    kwargs = patched_sync.call_args.kwargs
    assert kwargs["stock"] == 5
    assert kwargs["in_stock"] is True
    assert "остаток" in bot.sent[-1][1]


async def test_manual_sync_marks_sold_product_inactive(patched_sync):
    """Пометка «продано» скрывает товар — как в автосинхронизации канала."""
    bot = FakeBot()
    ctx = _ctx(bot)
    await fi.process_admin_sync(FakeMessage(TXT_SOLD), TXT_SOLD, ctx)

    kwargs = patched_sync.call_args.kwargs
    assert kwargs["in_stock"] is False


@pytest.mark.parametrize("word", ["продано", "нет в наличии", "sold",
                                  "закончился", "продана", "продан"])
async def test_manual_sync_sold_keywords_match_channel_path(word, patched_sync):
    """Список ключевых слов не должен разъезжаться с posts.py."""
    text = f"Камень, 6 мм\nАртикул 3333\nЦена 100\n{word}"
    await fi.process_admin_sync(FakeMessage(text), text, _ctx(FakeBot()))
    assert patched_sync.call_args.kwargs["in_stock"] is False


async def test_sold_keywords_are_in_sync_with_posts():
    """Единый источник истины: слова в fsm_inputs == слова в posts.py."""
    import bot.handlers.posts as posts

    def keywords_of(func):
        tree = ast.parse(inspect.getsource(func))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "sold_keywords":
                        return [e.value for e in node.value.elts]
        return None

    manual = keywords_of(fi.process_admin_sync)
    channel = keywords_of(posts._sync_post)
    assert manual, "sold_keywords пропали из process_admin_sync"
    assert manual == channel, f"расхождение списков: {manual} != {channel}"


# ------------------------- 3. загрузка видео -------------------------

async def test_manual_sync_uploads_video_to_max(patched_sync, monkeypatch):
    """Видео альбома обязаны загружаться в Max (раньше путь был отключён)."""
    upload_video = AsyncMock(return_value="VIDEOTOK")
    monkeypatch.setattr(fi, "upload_video_to_max", upload_video)

    await fi.process_admin_sync(
        FakeMessage(TXT_PLAIN), TXT_PLAIN, _ctx(FakeBot()),
        photos=["P1"], videos=["V1", "V2"],
    )

    assert upload_video.await_count == 2
    assert patched_sync.call_args.kwargs["max_video_ids"] == "VIDEOTOK,VIDEOTOK"


async def test_manual_sync_survives_video_upload_failure(patched_sync, monkeypatch):
    """Неудача загрузки видео не роняет добавление товара.

    Реальная ``upload_video_to_max`` глотает исключения и возвращает None
    (проверяется в tests/test_max_video_upload.py), поэтому здесь имитируем
    именно её штатный результат при сбое Max.
    """
    monkeypatch.setattr(fi, "upload_video_to_max", AsyncMock(return_value=None))

    bot = FakeBot()
    await fi.process_admin_sync(
        FakeMessage(TXT_PLAIN), TXT_PLAIN, _ctx(bot), photos=["P1"], videos=["V1"],
    )

    kwargs = patched_sync.call_args.kwargs
    assert kwargs["max_video_ids"] is None
    assert kwargs["max_photo_ids"] == "PHOTOTOK", "фото не должны страдать от видео"
    assert "Добавлен товар" in bot.sent[-1][1]


def test_video_path_is_not_commented_out():
    """Регрессия на «временно отключено»: вызов должен быть живым кодом."""
    src = inspect.getsource(fi.process_admin_sync)
    live = [ln for ln in src.splitlines() if "upload_video_to_max(" in ln
            and not ln.strip().startswith("#")]
    assert live, "загрузка видео снова закомментирована"


# --------------------- 4. отчёты и last_sync_msg_id ---------------------

async def test_report_message_id_is_remembered(patched_sync):
    """id отчёта сохраняется, чтобы следующий прогон его удалил."""
    bot = FakeBot()
    ctx = _ctx(bot)
    await fi.process_admin_sync(FakeMessage(TXT_PLAIN), TXT_PLAIN, ctx)
    first_id = ctx.user_data["last_sync_msg_id"]
    assert isinstance(first_id, int) and first_id > 0

    await fi.process_admin_sync(FakeMessage(TXT_STOCK), TXT_STOCK, ctx)
    second_id = ctx.user_data["last_sync_msg_id"]

    # Предыдущий отчёт удалён, новый — запомнен.
    assert (1, first_id) in bot.deleted
    assert second_id != first_id
    assert second_id == bot._mid, "запомнен должен быть id последнего отчёта"


def test_last_sync_msg_id_is_written_not_only_read():
    """Регрессия: раньше ключ только читался — ветка удаления была мёртвой."""
    src = inspect.getsource(fi.process_admin_sync)
    assert "user_data['last_sync_msg_id'] =" in src or            'user_data["last_sync_msg_id"] =' in src,         "last_sync_msg_id снова только читается"


async def test_first_sync_has_nothing_to_delete(patched_sync):
    """Первая синхронизация не пытается удалить несуществующее сообщение."""
    bot = FakeBot()
    await fi.process_admin_sync(FakeMessage(TXT_PLAIN), TXT_PLAIN, _ctx(bot))
    assert bot.deleted == []


# ------------------------- прочее поведение пути -------------------------

async def test_source_message_deleted_after_success(patched_sync):
    """Сообщение админа удаляется только при успешном добавлении."""
    msg = FakeMessage(TXT_PLAIN)
    await fi.process_admin_sync(msg, TXT_PLAIN, _ctx(FakeBot()))
    assert msg.deleted is True


async def test_sync_counter_increments(patched_sync):
    """Счётчик для итогового отчёта «Добавлено товаров: N»."""
    ctx = _ctx(FakeBot())
    await fi.process_admin_sync(FakeMessage(TXT_PLAIN), TXT_PLAIN, ctx)
    assert ctx.user_data["sync_count"] == 1


async def test_non_admin_is_ignored(patched_sync):
    await fi.process_admin_sync(
        FakeMessage(TXT_PLAIN, user_id=42), TXT_PLAIN,
        types.SimpleNamespace(bot=FakeBot(), user_data={}),
    )
    assert patched_sync.await_count == 0


async def test_duplicate_product_is_skipped(monkeypatch):
    """Существующий товар не перезаписывается, админ видит предупреждение."""
    existing = types.SimpleNamespace(id=9, name="Агат", article="1111")

    async def session_with_existing():
        class S:
            async def execute(self, *a, **kw):
                return types.SimpleNamespace(
                    scalars=lambda: types.SimpleNamespace(all=lambda: [existing])
                )
        yield S()

    monkeypatch.setattr(fi, "get_session", session_with_existing)
    upsert = AsyncMock()
    monkeypatch.setattr(fi, "upsert_product", upsert)

    bot = FakeBot()
    ctx = _ctx(bot)
    msg = FakeMessage(TXT_STOCK)
    await fi.process_admin_sync(msg, TXT_STOCK, ctx)

    assert upsert.await_count == 0
    assert "уже существует" in bot.sent[-1][1]
    assert ctx.user_data["sync_skipped"] == 1
    assert msg.deleted is True, "дубликат тоже убирается из чата админа"
