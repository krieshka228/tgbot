# Telegram/Max магазин-бот (tgbot)

Автоматический бот-магазин: читает карточки товаров из Telegram-канала, сохраняет их в PostgreSQL, принимает заказы в комментариях, ведёт корзину и оплату, а также передаёт медиа в Max.

## Что делает

- импорт товаров из постов Telegram-канала: название, артикул, цена, фото, видео;
- обработка комментариев с количеством и добавление товара в корзину;
- корзина, подтверждение заказа, реквизиты оплаты, чеки, статус оплаты;
- напоминания и Excel-отчёты;
- загрузка медиа в Max и синхронизация карточек с Max-каналом;
- опциональная интеграция с Google Sheets;
- PostgreSQL и Docker.

## Структура

- `bot/` — основное приложение;
- `bot/handlers/` — команды, FSM, корзина, заказы и обработка постов;
- `bot/db.py` — модели и миграции;
- `tests/` — pytest-тесты;
- `Dockerfile` — production-образ;
- `.env.example` — шаблон окружения.

## Быстрый запуск локально

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# заполните BOT_TOKEN, MAX_BOT_TOKEN, ID чатов и DATABASE_URL

python -m bot.main
```

## Запуск в Docker

```bash
cp .env.example .env
# заполните переменные окружения

docker build -t tgbot .
docker run -d --name tgbot --restart unless-stopped \
  --network host --env-file .env \
  -v "$PWD/secrets:/app/secrets:ro" tgbot
```

## Обязательное окружение

| Переменная | Назначение |
|---|---|
| `BOT_TOKEN` | токен Telegram-бота |
| `MAX_BOT_TOKEN` | токен Max-бота |
| `ADMIN_USER_ID` | Telegram user_id администратора |
| `ADMIN_CHAT_ID` | чат уведомлений |
| `CHANNEL_ID` | Telegram-канал с товарами |
| `DISCUSSION_GROUP_ID` | группа обсуждения канала |
| `DATABASE_URL` | PostgreSQL, SQLAlchemy asyncpg URL |
| `TG_PROXY` | HTTP-прокси для Telegram API (если нужен) |

Опционально: `TARGET_CHANNEL_ID` (публикация в целевой канал), `GSHEETS_*`.

## Тесты

```bash
source .venv/bin/activate
pytest
```

## Эксплуатация

Полезные команды:

```bash
docker logs -f tgbot
docker restart tgbot
docker exec -it tgbot sh
```

Логи хранятся в stdout контейнера; для долгого хранения подключайте внешний log collector.

## Безопасность

`.env`, `secrets/` и `credentials.json` не попадают в Git. Токены не выводятся в логах.
