"""Недоступный личный чат — ожидаемая ситуация, а не авария приложения."""
import logging
from collections import OrderedDict
from datetime import datetime, timezone

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from api import expired_callback, private_recipient_unavailable

log = logging.getLogger(__name__)


class PrivateChatGuard(BaseMiddleware):
    def __init__(self, limit=10000):
        self.unavailable = OrderedDict()
        self.limit = limit

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        if not message or message.chat.type != "private":
            return await handler(event, data)
        chat_id = message.chat.id
        previous = self.unavailable.get(chat_id)
        # После первого отказа пропускаем старую очередь этого пользователя.
        # Новое сообщение после разблокировки снова проверяется обычным способом.
        if previous and not callback and message.date <= previous:
            return
        if previous:
            self.unavailable.pop(chat_id, None)
        try:
            return await handler(event, data)
        except Exception as exc:
            if private_recipient_unavailable(exc):
                self.unavailable[chat_id] = datetime.now(timezone.utc)
                self.unavailable.move_to_end(chat_id)
                while len(self.unavailable) > self.limit:
                    self.unavailable.popitem(last=False)
                log.info("Личный чат %s недоступен: ответ пропущен; старые сообщения этого чата не повторяются.", chat_id)
                return
            if callback and expired_callback(exc):
                log.info("Устаревшая callback-кнопка: повторный ответ не отправляется.")
                return
            raise
