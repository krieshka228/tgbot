"""
scripts/seed_db.py — инициализация схемы БД и заполнение тестовыми данными.

Запуск::

    python -m scripts.seed_db            # создать схему + тестовые товары
    python -m scripts.seed_db --schema   # только создать схему (без данных)

Схема создаётся через ``Base.metadata.create_all`` (тот же путь, что и
``init_db`` при старте бота). Для production-миграций со сложной эволюцией
схемы рекомендуется Alembic:

    pip install alembic
    alembic init migrations
    # настроить sqlalchemy.url = DATABASE_URL и target_metadata = Base.metadata
    alembic revision --autogenerate -m "init"
    alembic upgrade head

Так как текущая схема создаётся декларативно, отдельные миграции пока не
обязательны, но Alembic — рекомендуемый следующий шаг при изменениях моделей.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from bot.cache import invalidate_catalog_cache
from bot.db import AsyncSession, Product, engine, init_db
from bot.logging_config import setup_logging

logger = logging.getLogger(__name__)

# Минимальный демонстрационный каталог.
_SAMPLE_PRODUCTS = [
    dict(post_id="seed_1", name="Платье вечернее, красное", price=5000.0,
         article="DRS-001", category="Женская одежда", stock=4,
         description="Шёлковое вечернее платье."),
    dict(post_id="seed_2", name="Платье вечернее, синее", price=5200.0,
         article="DRS-002", category="Женская одежда", stock=2,
         description="Синее платье в пол."),
    dict(post_id="seed_3", name="Рубашка мужская, белая", price=2500.0,
         article="SHT-010", category="Мужская одежда", stock=10,
         description="Классическая хлопковая рубашка."),
    dict(post_id="seed_4", name="Кроссовки беговые", price=7800.0,
         article="SNK-100", category="Обувь", stock=0,
         description="Распроданы — будут скрыты от покупателей."),
]


async def seed(with_data: bool = True) -> None:
    """Создаёт схему и (опционально) наполняет её тестовыми товарами."""
    await init_db()
    logger.info("schema created")

    if not with_data:
        return

    async with AsyncSession(engine, expire_on_commit=False) as session:
        added = 0
        for data in _SAMPLE_PRODUCTS:
            exists = (
                await session.execute(
                    select(Product).where(Product.post_id == data["post_id"])
                )
            ).scalar_one_or_none()
            if exists:
                continue
            stock = data["stock"]
            session.add(
                Product(
                    in_stock=stock > 0,
                    is_active=stock > 0,
                    **data,
                )
            )
            added += 1
        await session.commit()
    invalidate_catalog_cache()
    logger.info("seed complete", extra={"added": added})


def main() -> None:
    parser = argparse.ArgumentParser(description="Инициализация и наполнение БД")
    parser.add_argument(
        "--schema", action="store_true", help="только создать схему, без тестовых данных"
    )
    args = parser.parse_args()

    setup_logging(level="INFO", json_format=False)
    asyncio.run(seed(with_data=not args.schema))


if __name__ == "__main__":
    main()
