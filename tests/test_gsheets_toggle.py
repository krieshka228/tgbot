"""Тесты выключателя интеграции Google Sheets (settings.gsheets_active).

Интеграция должна быть неактивной, если:
* GSHEETS_ENABLED=false — явный выключатель;
* нет GOOGLE_SHEET_ID;
* нет GOOGLE_CREDENTIALS_PATH;
* файл ключа не существует на диске (главная причина сбоя: джоба падала
  каждые 5 минут с FileNotFoundError, хотя по конфигурации «всё задано»).

При этом отсутствие ключа НЕ должно мешать запуску бота: оформление заказа
обязано работать, просто очередь в таблицу не наполняется.
"""

import inspect

import pytest

import bot.gsheets as gs
from bot.config import Settings

_BASE = dict(_env_file=None)


class _FakeSettings:
    """Замена settings для проверки веток «выключено/включено».

    ``gsheets_active`` — вычисляемое свойство pydantic-модели, поэтому
    подменить его через monkeypatch.setattr нельзя (нет сеттера). Вместо
    этого подменяем весь объект settings в модуле gsheets.
    """

    def __init__(self, active: bool, interval: int = 5, reason: str = "test"):
        self.gsheets_active = active
        self.gsheets_sync_interval_minutes = interval
        self.gsheets_inactive_reason = reason
        self.google_credentials_path = "/nonexistent/credentials.json"
        self.admin_chat_id = 0


def _settings(**kwargs) -> Settings:
    return Settings(**_BASE, **kwargs)


def test_default_flag_is_enabled():
    """По умолчанию интеграция не выключена (управляется наличием ключа)."""
    assert _settings().gsheets_enabled is True


def test_explicit_false_disables(tmp_path):
    cred = tmp_path / "credentials.json"
    cred.write_text("{}", encoding="utf-8")
    s = _settings(
        gsheets_enabled=False,
        google_sheet_id="SHEET123",
        google_credentials_path=str(cred),
    )
    assert s.gsheets_credentials_present is True, "файл есть — сам по себе он валиден"
    assert s.gsheets_active is False, "но флаг false должен выключать всё"
    assert "GSHEETS_ENABLED=false" in s.gsheets_inactive_reason


def test_missing_credentials_file_disables(tmp_path):
    """Файл ключа отсутствует — интеграция неактивна, причина понятна."""
    missing = str(tmp_path / "credentials.json")
    s = _settings(google_sheet_id="SHEET123", google_credentials_path=missing)
    assert s.gsheets_credentials_present is False
    assert s.gsheets_active is False
    assert "файл ключа не найден" in s.gsheets_inactive_reason


def test_missing_sheet_id_disables(tmp_path):
    cred = tmp_path / "credentials.json"
    cred.write_text("{}", encoding="utf-8")
    s = _settings(google_sheet_id="", google_credentials_path=str(cred))
    assert s.gsheets_active is False
    assert "GOOGLE_SHEET_ID" in s.gsheets_inactive_reason


def test_empty_credentials_path_disables():
    s = _settings(google_sheet_id="SHEET123", google_credentials_path="")
    assert s.gsheets_credentials_present is False
    assert s.gsheets_active is False
    assert "GOOGLE_CREDENTIALS_PATH" in s.gsheets_inactive_reason


def test_all_present_activates(tmp_path):
    cred = tmp_path / "credentials.json"
    cred.write_text("{}", encoding="utf-8")
    s = _settings(google_sheet_id="SHEET123", google_credentials_path=str(cred))
    assert s.gsheets_active is True


def test_env_string_false_is_parsed_as_bool():
    """GSHEETS_ENABLED=false из .env — строка, pydantic должен привести к bool."""
    s = Settings(**_BASE, gsheets_enabled="false")
    assert s.gsheets_enabled is False
    assert s.gsheets_active is False


async def test_enqueue_order_noop_when_inactive(monkeypatch):
    """При выключенной интеграции enqueue_order ничего не пишет в БД."""
    monkeypatch.setattr(gs, "settings", _FakeSettings(active=False))
    calls = []

    class _FakeSession:
        async def execute(self, *a, **kw):
            calls.append(a)
            raise AssertionError("не должно быть обращений к БД")

        async def commit(self):
            calls.append("commit")

    added = await gs.enqueue_order(_FakeSession(), 42)
    assert added == 0
    assert calls == []


def test_sync_job_guards_on_active():
    """Джоба проверяет gsheets_active, а не отдельные части конфига."""
    src = inspect.getsource(gs.gsheets_sync_job)
    assert "gsheets_active" in src
    assert "settings.gsheets_enabled" not in src


def test_register_jobs_guards_on_active():
    src = inspect.getsource(gs.register_jobs)
    assert "gsheets_active" in src
    assert "settings.gsheets_enabled" not in src


def test_register_jobs_does_not_schedule_when_inactive(monkeypatch):
    """Без ключа джоба НЕ регистрируется — иначе шум каждые N минут."""
    monkeypatch.setattr(gs, "settings", _FakeSettings(active=False))

    class _FakeJobQueue:
        def __init__(self):
            self.scheduled = []

        def run_repeating(self, *a, **kw):
            self.scheduled.append(kw)

    app = type("A", (), {"job_queue": _FakeJobQueue()})()
    gs.register_jobs(app)
    assert app.job_queue.scheduled == []


def test_register_jobs_schedules_when_active(monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "settings", _FakeSettings(active=True, interval=5))

    class _FakeJobQueue:
        def __init__(self):
            self.scheduled = []

        def run_repeating(self, *a, **kw):
            self.scheduled.append(kw)

    app = type("A", (), {"job_queue": _FakeJobQueue()})()
    gs.register_jobs(app)
    assert len(app.job_queue.scheduled) == 1
    assert app.job_queue.scheduled[0]["interval"] == 300


def test_checkout_handler_checks_flag():
    """Хендлер оформления заказа не наполняет выключенную очередь."""
    from bot.handlers import checkout

    src = inspect.getsource(checkout)
    assert "gsheets_active" in src
