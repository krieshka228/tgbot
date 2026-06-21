"""
config.py — конфигурация бота через переменные окружения.

Значения читаются из окружения и (опционально) из файла ``.env`` с помощью
``pydantic-settings`` — это даёт типобезопасность и валидацию ещё до запуска
бота. Класс :class:`Settings` — единый источник истины.

Для обратной совместимости со старым кодом ниже экспортируются
модуль-уровневые константы (``BOT_TOKEN``, ``ADMIN_USER_ID`` и т.д.).
Новый код может импортировать как сами константы, так и объект ``settings``.

ВАЖНО (безопасность): секреты (``BOT_TOKEN``) НЕ хранятся в коде и НЕ
коммитятся — только в ``.env`` (он в ``.gitignore`` и ``.dockerignore``).
Перед запуском в продакшене вызывается :meth:`Settings.assert_production_ready`,
которая падает с понятной ошибкой, если обязательные секреты не заданы.
"""

from __future__ import annotations
from pathlib import Path
from typing import ClassVar

import logging
import re

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Токен бота от @BotFather имеет вид "<digits>:<35+ символов>".
_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")


class Settings(BaseSettings):
    """Валидированная конфигурация бота.

    Поля заполняются из переменных окружения (регистр игнорируется) и из
    ``.env``. Значения по умолчанию допускают импорт модуля без окружения
    (нужно для тестов); строгая проверка обязательных полей выполняется
    отдельно в :meth:`assert_production_ready`.
    """
    BASE_DIR: ClassVar[Path] = Path(__file__).parent.parent
    ENV_FILE: ClassVar[Path] = BASE_DIR / ".env"

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    bot_token: str = Field(default="", description="Токен от @BotFather")
    max_bot_token: str = Field(default="", description="Токен Max-бота для загрузки медиа")
    admin_user_id: int = Field(default=0, description="Telegram ID администратора")
    admin_chat_id: int = Field(default=0, description="Чат для уведомлений админа")
    channel_id: int = Field(default=0, description="ID канала с товарами")
    discussion_group_id: int = Field(
        default=0, description="ID группы обсуждения канала (для комментариев)"
    )
    database_url: str = Field(
        default="sqlite+aiosqlite:///orders.db",
        description="DSN базы данных (async-драйвер)",
    )

    # --- Эксплуатационные настройки (не секреты) ---
    log_level: str = Field(default="INFO", description="DEBUG/INFO/WARNING/ERROR")
    log_json: bool = Field(default=True, description="Структурированные JSON-логи")

    # Антифлуд: не более N сообщений за окно в секундах на одного пользователя.
    rate_limit_messages: int = Field(default=20, ge=1)
    rate_limit_window: float = Field(default=10.0, gt=0)

    @field_validator("log_level")
    @classmethod
    def _normalize_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Недопустимый LOG_LEVEL: {value!r}")
        return level

    @field_validator("admin_user_id", "admin_chat_id", "channel_id", "discussion_group_id")
    @classmethod
    def _non_negative_ids(cls, value: int, info: ValidationInfo) -> int:
        # channel_id у каналов отрицательный (-100...), поэтому проверяем
        # только идентификаторы пользователей/чатов администратора.
        if info.field_name in {"admin_user_id", "admin_chat_id"} and value < 0:
            raise ValueError(f"{info.field_name} должен быть неотрицательным")
        return value

    @model_validator(mode="after")
    def _default_admin_chat(self) -> "Settings":
        # Если ADMIN_CHAT_ID не задан, шлём уведомления в личку администратора.
        if not self.admin_chat_id and self.admin_user_id:
            object.__setattr__(self, "admin_chat_id", self.admin_user_id)
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def assert_production_ready(self) -> None:
        """Проверяет, что заданы все обязательные для запуска значения.

        Вызывается на старте бота (fail-fast). Бросает ``RuntimeError`` с
        перечнем проблем, чтобы бот не поднимался в заведомо нерабочей
        конфигурации.
        """
        problems: list[str] = []
        if not self.bot_token:
            problems.append("BOT_TOKEN не задан")
        elif not _TOKEN_RE.match(self.bot_token):
            problems.append("BOT_TOKEN имеет неверный формат")
        if not self.admin_user_id:
            problems.append("ADMIN_USER_ID не задан")
        if not self.channel_id:
            problems.append("CHANNEL_ID не задан")
        if problems:
            raise RuntimeError(
                "Некорректная конфигурация бота: " + "; ".join(problems)
            )


# Единственный экземпляр настроек на процесс.
settings = Settings()

# --- Обратная совместимость: модуль-уровневые константы ---
BOT_TOKEN: str = settings.bot_token
ADMIN_USER_ID: int = settings.admin_user_id
ADMIN_CHAT_ID: int = settings.admin_chat_id
CHANNEL_ID: int = settings.channel_id
DATABASE_URL: str = settings.database_url
DISCUSSION_GROUP_ID: int = settings.discussion_group_id
