"""Проверяет подписки до каждого события в личном чате пользователя."""
from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from content import valid_id


def action_for(event):
    if isinstance(event, CallbackQuery):
        if (event.data or "").startswith("check:"):
            return {"kind": "download", "key": event.data.partition(":")[2]}
        if (event.data or "").startswith("a:"):
            return {"kind": "admin"}
        return {"kind": "home"}
    parts = (event.text or "").split(maxsplit=1)
    command = parts[0].split("@")[0] if parts else ""
    if command == "/id":
        return {"kind": "id"}
    if command in ("/admin", "/diag", "/resume", "/cancel"):
        return {"kind": "admin"}
    if command == "/start" and len(parts) == 2:
        payload = parts[1].strip()
        preview = payload.startswith("p_")
        key = payload[2:] if preview else payload
        try:
            valid_id(key)
        except ValueError:
            return {"kind": "home"}
        return {"kind": "preview" if preview else "download", "key": key}
    return {"kind": "home"}


class SubscriptionGate(BaseMiddleware):
    def __init__(self, service):
        self.service = service

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        user = event.from_user
        if not message or message.chat.type != "private" or not user:
            return await handler(event, data)
        is_admin = user.id in self.service.settings.admin_ids
        # Эта кнопка всегда доступна, иначе нельзя повторно подтвердить подписку.
        if callback and (event.data or "").startswith("sub:"):
            return await handler(event, data)
        # Единственная служебная возможность выключить проверку ДЛЯ СЕБЯ:
        # доступна только администратору, даже когда он не подписан.
        command = (event.text or "").split(maxsplit=1)[0].split("@")[0] if not callback and event.text else ""
        if is_admin and (command == "/mysub" or
                         (callback and (event.data or "") == "a:togglemycheck")):
            return await handler(event, data)
        if is_admin and not self.service.db.admin_subscription_enabled(user.id):
            data["subscription_checked"] = True
            return await handler(event, data)
        if not await self.service.require_subscription(user.id, action_for(event)):
            if callback:
                try:
                    await event.answer("Для продолжения подтвердите подписку.")
                except TelegramAPIError:
                    pass
            return
        data["subscription_checked"] = True
        return await handler(event, data)
