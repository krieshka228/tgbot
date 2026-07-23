from telegram.ext import MessageHandler, filters

class StateFilter(filters.MessageFilter):
    """Фильтр, проверяющий состояние пользователя в application.user_data."""
    def __init__(self, state: str):
        super().__init__()
        self.state = state

    def filter(self, message) -> bool:
        if not message.from_user:
            return False
        # Получаем объект бота через get_bot()
        bot = message.get_bot()
        if not bot:
            return False
        user_data = bot.application.user_data.get(message.from_user.id, {})
        return user_data.get('state') == self.state