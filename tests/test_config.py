"""Тесты валидации конфигурации (bot.config.Settings)."""

import pytest

from bot.config import Settings

# _env_file=None — игнорируем реальный .env, чтобы тесты были детерминированы.
_BASE = dict(_env_file=None)


def _settings(**kwargs) -> Settings:
    return Settings(**_BASE, **kwargs)


def test_admin_chat_defaults_to_admin_user():
    s = _settings(admin_user_id=42, admin_chat_id=0)
    assert s.admin_chat_id == 42


def test_log_level_normalized():
    assert _settings(log_level="debug").log_level == "DEBUG"


def test_invalid_log_level_raises():
    with pytest.raises(Exception):
        _settings(log_level="LOUD")


def test_assert_production_ready_missing_token():
    s = _settings(bot_token="", admin_user_id=1, channel_id=-100123)
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        s.assert_production_ready()


def test_assert_production_ready_bad_token_format():
    s = _settings(bot_token="not-a-token", admin_user_id=1, channel_id=-100)
    with pytest.raises(RuntimeError, match="формат"):
        s.assert_production_ready()


def test_assert_production_ready_ok():
    s = _settings(
        bot_token="123456789:AAH" + "x" * 32,
        admin_user_id=1,
        channel_id=-1001234567890,
    )
    s.assert_production_ready()  # не должно бросать


def test_is_sqlite():
    assert _settings(database_url="sqlite+aiosqlite:///x.db").is_sqlite is True
    assert _settings(database_url="postgresql+asyncpg://u@h/db").is_sqlite is False
