"""Минимальные ответы getChat/getChatMember: не зависят от новых полей Bot API."""
from typing import Any

from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError)
from aiogram.methods import TelegramMethod


class GetChatInfo(TelegramMethod[dict[str, Any]]):
    __returning__ = dict[str, Any]
    __api_method__ = "getChat"
    chat_id: int | str


class GetMemberInfo(TelegramMethod[dict[str, Any]]):
    __returning__ = dict[str, Any]
    __api_method__ = "getChatMember"
    chat_id: int | str
    user_id: int


def as_dict(value):
    return value if isinstance(value, dict) else value.model_dump(mode="json", exclude_none=True)


def private_recipient_unavailable(exc):
    """Ошибки личного получателя отличаем от запрета доступа к каналу."""
    if not isinstance(exc, (TelegramForbiddenError, TelegramBadRequest)):
        return False
    method = getattr(exc, "method", None)
    chat = getattr(method, "chat_id", None)
    if not (type(chat) is int and chat > 0):
        return False
    api_method = getattr(method, "__api_method__", "")
    if not api_method.startswith(("send", "copy", "forward", "editMessage")):
        return False
    message = exc.message.casefold()
    return any(reason in message for reason in (
        "bot was blocked by the user", "user is deactivated", "user_is_deactivated",
        "bot can't initiate conversation with a user", "chat not found",
        "bot can't send messages to bots"))


def expired_callback(exc):
    return (isinstance(exc, TelegramBadRequest)
            and getattr(getattr(exc, "method", None), "__api_method__", "") == "answerCallbackQuery"
            and any(s in exc.message.casefold() for s in ("query is too old", "query id is invalid", "query_id_invalid")))


def explain_error(exc):
    """Различаем сеть, неверный ID, права и частые запросы; не выдаём токен."""
    if isinstance(exc, TelegramRetryAfter):
        return f"Лимит Telegram: повтори действие через {exc.retry_after} сек."
    if isinstance(exc, TelegramNetworkError):
        return "Нет соединения с Telegram. Проверь интернет/VPN; админство канала здесь ни при чём."
    if isinstance(exc, TelegramUnauthorizedError):
        return "Telegram не принял токен бота. Проверь BOT_TOKEN / TELEGRAM_BOT_TOKEN в окружении или .env."
    if private_recipient_unavailable(exc):
        return "Получатель заблокировал бота или личный чат недоступен. Это не ошибка прав канала."
    if isinstance(exc, TelegramForbiddenError):
        return "Telegram запретил доступ к выбранному чату. Проверь ID чата и доступ именно этого бота."
    if isinstance(exc, TelegramBadRequest):
        message = exc.message.lower()
        if "chat not found" in message:
            return "Канал или группа не найдены по указанному ID/@username. Проверь ID в «Каналы»; для группы можно использовать /storage в ней."
        if "user not found" in message or "participant_id_invalid" in message:
            return "Telegram не смог найти участника для проверки. Повтори запрос из личного чата с ботом."
        if any(s in message for s in ("not enough rights", "administrator rights", "chat_admin_required")):
            return "Для этого действия Telegram требует дополнительные права бота в выбранном чате."
        if "message is not modified" in message:
            return "Изменений нет: Telegram уже показывает эти данные."
        if "file_reference" in message or "wrong file" in message or "file identifier" in message:
            return "Telegram отклонил сохранённый file_id. Заново зарегистрируй или замени файл через админку."
        if "query is too old" in message:
            return "Кнопка устарела. Отправь /admin и повтори действие."
        if "custom emoji" in message or "custom_emoji" in message:
            return "Telegram отклонил premium-эмодзи. Проверь права на custom emoji; обычные эмодзи доступны всегда."
        return "Telegram: " + exc.message.removeprefix("Telegram server says - ")[:600]
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, TelegramAPIError):
        return "Сбой ответа Telegram. Повтори действие; подробности записаны в data/bot.log."
    return "Внутренняя ошибка. Подробности записаны в data/bot.log. Админка остаётся доступной: /admin."


class Channels:
    def __init__(self, bot):
        self.bot = bot

    async def chat(self, reference):
        value = as_dict(await self.bot(GetChatInfo(chat_id=reference)))
        if type(value.get("id")) is not int or value.get("type") not in ("channel", "group", "supergroup"):
            raise ValueError("Нужен канал, группа или супергруппа Telegram.")
        return value

    async def member(self, reference, user_id):
        value = as_dict(await self.bot(GetMemberInfo(chat_id=reference, user_id=user_id)))
        if value.get("status") not in ("creator", "administrator", "member", "restricted", "left", "kicked"):
            raise ValueError("Telegram вернул неизвестный статус подписки; файл не будет выдан до успешной проверки.")
        return value

    async def inspect(self, reference, purpose=None):
        if not reference:
            raise ValueError("Канал или чат ещё не привязан. Открой /admin → «Каналы».")
        chat = await self.chat(reference)
        member = await self.member(chat["id"], self.bot.id)
        if purpose == "storage" and chat["type"] in ("group", "supergroup"):
            status = member["status"]
            if status in ("administrator", "creator"):
                return chat, member
            if status not in ("member", "restricted") or (status == "restricted" and member.get("is_member") is not True):
                raise ValueError("Бот не состоит в группе-хранилище. Добавь его в этот чат.")
            permissions = member if status == "restricted" else (chat.get("permissions") or {})
            documents = permissions.get("can_send_documents", permissions.get("can_send_media_messages"))
            if documents is False or (status == "restricted" and documents is not True):
                raise ValueError("В группе-хранилище боту запрещена отправка документов. Разреши её или назначь бота администратором.")
            return chat, member
        if chat["type"] != "channel":
            raise ValueError("Для основного канала и каналов-спонсоров нужен именно канал. Группа подходит для хранилища файлов.")
        if member["status"] not in ("administrator", "creator"):
            raise ValueError(f"Бот с ID {self.bot.id} не администратор канала {chat.get('title', '')} ({chat['id']}).")
        if purpose in ("main", "storage") and member.get("can_post_messages") is not True and member["status"] != "creator":
            raise ValueError(f"В канале {chat.get('title', '')} бот администратор, но право публикации отключено.")
        return chat, member

    async def report(self, reference, purpose=None):
        try:
            chat, member = await self.inspect(reference, purpose)
            types = {"channel": "канал", "group": "группа", "supergroup": "супергруппа"}
            lines = [f"Чат: {chat.get('title', chat['id'])}", f"Тип: {types[chat['type']]}",
                     f"ID: {chat['id']}", f"Статус бота: {member['status']}"]
            if chat["type"] in ("group", "supergroup"):
                lines += ["Отправка документов: ограничений не обнаружено",
                          f"Удаление чужих сообщений: {'да' if member.get('can_delete_messages') or member['status']=='creator' else 'нет'}"]
            elif purpose in ("main", "storage"):
                lines += [f"Публикация: {'да' if member.get('can_post_messages') else 'нет'}",
                          f"Редактирование чужих постов: {'да' if member.get('can_edit_messages') else 'нет'}",
                          f"Удаление: {'да' if member.get('can_delete_messages') else 'нет'}"]
            return True, "\n".join(lines)
        except (TelegramAPIError, ValueError) as exc:
            return False, explain_error(exc)
