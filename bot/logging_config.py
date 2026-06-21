"""
logging_config.py — настройка структурированного логирования.

В продакшене логи удобнее собирать и фильтровать в JSON-формате (одна
строка — один JSON-объект): это легко парсится агрегаторами (Loki, ELK,
CloudWatch и т.п.). Для локальной разработки можно отключить JSON
(``LOG_JSON=false``) и получить обычный человекочитаемый вывод.

Уровни:
    * DEBUG   — детали потока (содержимое апдейтов, шаги FSM);
    * INFO    — ключевые события (запуск/остановка, действия пользователей);
    * ERROR   — сбои с трейсбеком.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging

# Стандартные атрибуты LogRecord, которые НЕ нужно дублировать в поле extra.
_RESERVED = set(
    logging.makeLogRecord({}).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    """Форматтер, превращающий ``LogRecord`` в одну JSON-строку.

    Любые дополнительные поля, переданные через ``logger.info(..., extra={})``,
    попадают в JSON как отдельные ключи — это и есть «структурированные» логи.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _dt.datetime.fromtimestamp(
                record.created, tz=_dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        # Прокидываем пользовательские поля (extra=...) без перезаписи базовых.
        for key, value in record.__dict__.items():
            if key in _RESERVED or key in payload:
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = repr(value)

        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str = "INFO", json_format: bool = True) -> None:
    """Конфигурирует корневой логгер один раз на процесс.

    :param level: текстовый уровень (``DEBUG``/``INFO``/``WARNING``/``ERROR``).
    :param json_format: ``True`` — JSON-строки, ``False`` — обычный текст.
    """
    handler = logging.StreamHandler()
    if json_format:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # httpx/telegram очень болтливы на INFO — приглушаем фоновый шум.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
