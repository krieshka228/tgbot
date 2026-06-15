import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_USER_ID: int = int(os.getenv("ADMIN_USER_ID", "0"))
ADMIN_CHAT_ID: int = int(os.getenv("ADMIN_CHAT_ID", "0"))
CHANNEL_ID: int = int(os.getenv("CHANNEL_ID", "0"))
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///orders.db")

# ID группы обсуждения канала (если есть). Используется для обработки комментариев.
DISCUSSION_GROUP_ID: int = int(os.getenv("DISCUSSION_GROUP_ID", "0"))