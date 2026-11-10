"""Компактные панели: кнопки редактируются, ответы на ввод появляются внизу чата.

Только последняя текстовая панель управляется этим модулем. Сообщения
предпросмотра, документы и сообщения Telegram не трогаются.
"""
import logging
from collections import OrderedDict

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery

log = logging.getLogger(__name__)
# chat_id -> (panel_message_id, input_message_id)
# input_message_id позволяет заменить панель при нескольких panel() за один шаг.
_last_panels = OrderedDict()


def message_of(event):
    return event.message if isinstance(event, CallbackQuery) else event


def chat_id_of(event):
    return message_of(event).chat.id


def remember_panel(chat_id, message_id, source_message_id=None):
    if not message_id:
        return
    _last_panels[chat_id] = (message_id, source_message_id)
    _last_panels.move_to_end(chat_id)
    while len(_last_panels) > 1000:
        _last_panels.popitem(last=False)


async def _try_edit(bot, chat_id, message_id, kwargs):
    try:
        return await bot.edit_message_text(chat_id=chat_id, message_id=message_id, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return True
        log.debug("Нельзя обновить панель %s: %s", message_id, exc)
    except (TelegramAPIError, AttributeError, TypeError) as exc:
        log.debug("Не удалось обновить панель %s: %s", message_id, exc)
    return None


async def _retire_panel(bot, chat_id, old_id):
    """Удаляем только свою ранее запомненную текстовую панель."""
    if not old_id:
        return
    try:
        await bot.delete_message(chat_id, old_id)
    except (TelegramAPIError, AttributeError, TypeError):
        # У старой панели не должно оставаться живых кнопок.
        try:
            await bot.edit_message_reply_markup(chat_id, old_id, reply_markup=None)
        except (TelegramAPIError, AttributeError, TypeError):
            pass


async def panel(event, text, reply_markup=None, entities=None):
    msg = message_of(event)
    kwargs = dict(text=text, reply_markup=reply_markup, entities=entities, parse_mode=None)
    chat_id = msg.chat.id

    if isinstance(event, CallbackQuery):
        # Клик по кнопке: редактируем сообщение, на которое нажали.
        edited = await _try_edit(msg.bot, chat_id, msg.message_id, kwargs)
        if edited is not None:
            remember_panel(chat_id, msg.message_id)
            return edited
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except (TelegramAPIError, AttributeError, TypeError):
            pass
        old = _last_panels.get(chat_id)
        result = await msg.answer(**kwargs)
        if msg.chat.type == "private":
            remember_panel(chat_id, result.message_id)
            if old and old[0] != result.message_id:
                await _retire_panel(msg.bot, chat_id, old[0])
        return result

    if msg.chat.type == "private":
        last = _last_panels.get(chat_id)
        # Один обработчик иногда вызывает panel() дважды (например, при загрузке
        # документа). Только в этом случае редактируем уже созданный ответ.
        if last and last[1] == msg.message_id:
            edited = await _try_edit(msg.bot, chat_id, last[0], kwargs)
            if edited is not None:
                return edited
        # Новое сообщение администратора всегда получает новый ответ ВНИЗУ,
        # чтобы результат не оставался над набранным текстом.
        result = await msg.answer(**kwargs)
        remember_panel(chat_id, result.message_id, msg.message_id)
        if last and last[0] != result.message_id:
            await _retire_panel(msg.bot, chat_id, last[0])
        return result

    return await msg.answer(**kwargs)
