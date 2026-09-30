"""Тесты загрузки видео в Max (bot.utils.upload_video_to_max).

Регрессия: использовался aiomax.Bot с подменённой ClientSession без base_url —
aiomax шлёт относительный URL "uploads", поэтому каждый вызов падал с
aiohttp.InvalidUrlClientError: uploads, и товары сохранялись без max_video_ids.
"""

import inspect
from unittest.mock import AsyncMock, patch

import pytest

import bot.utils as utils


def test_upload_video_uses_absolute_max_api_url():
    """Видео-путь не должен полагаться на относительные URL aiomax."""
    src = inspect.getsource(utils._upload_video_file_to_max)
    assert "MAX_API_BASE" in src
    assert "uploads?type=video" in src


def test_upload_video_does_not_use_aiomax_session_swap():
    """Больше никакой подмены session у aiomax.Bot в видео-пути."""
    src = inspect.getsource(utils.upload_video_to_max)
    assert "max_bot.session" not in src
    assert "MaxBot(" not in src


class _FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status = status
        self._json = json_data or {}
        self._text = text

    async def text(self):
        return self._text

    async def json(self):
        return self._json

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Запоминает последовательность POST-запросов."""

    def __init__(self, uploads_response, upload_response):
        self._uploads = uploads_response
        self._upload = upload_response
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return self._uploads if "uploads" in url else self._upload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def fake_session():
    return _FakeSession(
        uploads_response=_FakeResp(200, {"url": "https://files.max.ru/up/1", "token": "VIDTOK"}),
        upload_response=_FakeResp(200, {}),
    )


async def test_video_upload_performs_both_steps(fake_session):
    """Токен из /uploads НЕ возвращается сразу — файл обязан быть отправлен."""
    with patch.object(utils.aiohttp, "ClientSession", return_value=fake_session), \
         patch.object(utils.aiohttp, "TCPConnector", return_value=None), \
         patch("builtins.open", create=True) as m_open:
        m_open.return_value.__enter__ = lambda s: s
        m_open.return_value.__exit__ = lambda *a: False
        m_open.return_value.read = lambda: b"video-bytes"
        token = await utils._upload_video_file_to_max("/tmp/x.mp4")

    assert token == "VIDTOK"
    assert len(fake_session.calls) == 2, "должно быть два запроса: /uploads и загрузка файла"
    assert fake_session.calls[1] == "https://files.max.ru/up/1"


async def test_video_upload_without_url_fails(fake_session):
    """Если /uploads не вернул url — не выдумываем токен."""
    fake_session._uploads = _FakeResp(200, {"token": "VIDTOK"})  # url отсутствует
    with patch.object(utils.aiohttp, "ClientSession", return_value=fake_session), \
         patch.object(utils.aiohttp, "TCPConnector", return_value=None):
        assert await utils._upload_video_file_to_max("/tmp/x.mp4") is None


async def test_video_upload_failure_on_second_step():
    """Ошибка на шаге загрузки файла — возвращаем None, а не токен."""
    sess = _FakeSession(
        uploads_response=_FakeResp(200, {"url": "https://files.max.ru/up/1", "token": "VIDTOK"}),
        upload_response=_FakeResp(500, text="server error"),
    )
    with patch.object(utils.aiohttp, "ClientSession", return_value=sess), \
         patch.object(utils.aiohttp, "TCPConnector", return_value=None), \
         patch("builtins.open", create=True) as m_open:
        m_open.return_value.__enter__ = lambda s: s
        m_open.return_value.__exit__ = lambda *a: False
        m_open.return_value.read = lambda: b"video-bytes"
        assert await utils._upload_video_file_to_max("/tmp/x.mp4") is None
