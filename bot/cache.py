"""
cache.py — простой in-memory кэш с TTL для данных каталога.

Каталог (категории / товары по категориям) запрашивается очень часто —
каждый покупатель, листающий каталог, дёргает базу на каждое нажатие
кнопки. Товары же обновляются редко (синхронизация из канала, ручное
изменение остатков администратором), поэтому имеет смысл недолго
кэшировать результаты выборок и сбрасывать кэш сразу при изменении
товаров.

Это простое решение для одного процесса (без Redis и т.п.) — для
текущего масштаба бота этого достаточно. Если бот будет запускаться в
нескольких процессах одновременно, кэш нужно будет вынести во внешнее
хранилище.
"""

import asyncio
import time
from typing import Any, Awaitable, Callable

DEFAULT_TTL = 60.0  # секунд

_cache: dict[str, tuple[float, Any]] = {}
_lock = asyncio.Lock()


async def get_or_set(key: str, loader: Callable[[], Awaitable[Any]], ttl: float = DEFAULT_TTL) -> Any:
    """Возвращает значение из кэша, если оно есть и не устарело,
    иначе вычисляет его через loader(), кэширует и возвращает."""
    now = time.monotonic()
    async with _lock:
        entry = _cache.get(key)
        if entry is not None and (now - entry[0]) < ttl:
            return entry[1]

    value = await loader()

    async with _lock:
        _cache[key] = (now, value)
    return value


def invalidate_catalog_cache() -> None:
    """Сбрасывает весь кэш каталога. Вызывается при любом изменении
    товаров (синхронизация, изменение остатков, удаление, заказ,
    отмена заказа и т.п.), чтобы покупатели не видели устаревшие данные."""
    _cache.clear()
