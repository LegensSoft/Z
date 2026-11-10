"""Храним текст, Telegram entities и file_id, ничего не скачивая на телефон."""
import logging
import re
from urllib.parse import urlsplit

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, InputMediaAnimation,
                           InputMediaPhoto, InputMediaVideo, Message, MessageEntity)

log = logging.getLogger(__name__)

ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
CAPTION_ID_RE = re.compile(r"(?im)^\s*id\s*:\s*([^\s]+)\s*$")


def valid_id(value: str):
    if not ID_RE.fullmatch(value) or value.startswith("p_"):
        raise ValueError("ID: 1–40 латинских букв, цифр, _ или -. Начало p_ зарезервировано.")
    return value


def caption_id(caption: str | None):
    matches = CAPTION_ID_RE.findall(caption or "")
    if len(matches) != 1:
        raise ValueError("В подписи нужна ровно одна отдельная строка: id: 123")
    return valid_id(matches[0])


def valid_url(value: str):
    value = value.strip()
    try:
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or any(c.isspace() for c in value)
                or len(value) > 2048):
            raise ValueError
        _ = parts.port
    except ValueError:
        raise ValueError("Пришли полную ссылку https://… или http://… без пробелов.") from None
    return value


def snapshot(message: Message):
    if message.media_group_id:
        raise ValueError("Альбомы не поддерживаются. Отправь одно фото или видео.")
    kind, file_id = "text", None
    if message.photo:
        kind, file_id = "photo", message.photo[-1].file_id
    elif message.animation:
        kind, file_id = "animation", message.animation.file_id
    elif message.video:
        kind, file_id = "video", message.video.file_id
    elif message.sticker:
        kind, file_id = "sticker", message.sticker.file_id
    elif message.text is None:
        raise ValueError("Пришли текст, фото/видео/GIF с подписью или отдельный стикер.")
    text = message.text if kind == "text" else (message.caption or "")
    entities = message.entities if kind == "text" else message.caption_entities
    return {"kind": kind, "file_id": file_id, "text": text,
            "entities": [e.model_dump(mode="json", exclude_none=True) for e in entities or []],
            "above": bool(message.show_caption_above_media),
            "spoiler": bool(message.has_media_spoiler)}


def has_custom_emoji(content):
    return any(entity.get("type") == "custom_emoji" for entity in content.get("entities", []))


def save_emoji_source(content, chat_id, message_id):
    """The source must have exactly the same rendered text/caption as the post.

    A raw copy can preserve Telegram markup that isn't accepted for a newly
    authored channel message. This is best-effort, not an API permission bypass.
    """
    # Для forwardMessage нужен оригинал любого поста, а не только Premium.
    content["emoji_source"] = {"chat_id": chat_id, "message_id": message_id}


def entities(content, custom_emoji=True):
    # Сохраняем UTF-16 offsets как прислал Telegram, не пересчитываем по Python len().
    return [MessageEntity.model_validate(e) for e in content["entities"]
            if custom_emoji or e["type"] != "custom_emoji"]


def url_keyboard(label, url):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, url=url)
    ]])


def deep_link(username, key, preview=False):
    valid_id(key)
    return f"https://t.me/{username}?start={'p_' if preview else ''}{key}"


async def send_content(bot, chat_id, content, keyboard=None, custom_emoji=True):
    common = {"chat_id": chat_id, "reply_markup": keyboard}
    # Telegram only officially permits direct custom-emoji entities in
    # channels for bots with an additional Fragment username. Try copying
    # the already composed private-chat preview without changing its caption.
    # copyMessage may still strip custom emojis server-side: it is NOT a
    # guaranteed workaround, and the caller can never verify their display.
    source = content.get("emoji_source") if has_custom_emoji(content) else None
    if (source and isinstance(source, dict) and
            type(source.get("chat_id")) is int and
            type(source.get("message_id")) is int and
            source["chat_id"] != chat_id):
        try:
            return await bot.copy_message(
                chat_id=chat_id, from_chat_id=source["chat_id"],
                message_id=source["message_id"], reply_markup=keyboard)
        except TelegramAPIError as exc:
            log.warning("Telegram refused copying original premium markup: %s", exc)
            # A bot with an additional Fragment username may still be able
            # to send the direct entities; let the server decide.
    kind = content["kind"]
    if kind == "text":
        return await bot.send_message(**common, text=content["text"],
                                      entities=entities(content, custom_emoji), parse_mode=None)
    if kind == "sticker":
        return await bot.send_sticker(**common, sticker=content["file_id"])
    common.update(caption=content["text"], caption_entities=entities(content, custom_emoji),
                  parse_mode=None, show_caption_above_media=content["above"],
                  has_spoiler=content["spoiler"])
    if kind == "photo":
        return await bot.send_photo(**common, photo=content["file_id"])
    if kind == "video":
        return await bot.send_video(**common, video=content["file_id"])
    return await bot.send_animation(**common, animation=content["file_id"])


async def edit_content(bot, post, content, keyboard, custom_emoji=True):
    old_kind, kind = post["content"]["kind"], content["kind"]
    common = {"chat_id": post["channel_id"], "message_id": post["message_id"], "reply_markup": keyboard}
    if old_kind == "sticker" or kind == "sticker":
        if old_kind == kind and content["file_id"] == post["content"]["file_id"]:
            return await bot.edit_message_reply_markup(**common)
        raise ValueError("Telegram не позволяет заменить этот стикер редактированием. Создай новый пост.")
    if kind == "text":
        if old_kind != "text":
            raise ValueError("Для поста с медиа отправь новое медиа с подписью. Для чистого текста создай новый пост.")
        return await bot.edit_message_text(**common, text=content["text"],
                                           entities=entities(content, custom_emoji), parse_mode=None)
    media_cls = {"photo": InputMediaPhoto, "video": InputMediaVideo, "animation": InputMediaAnimation}[kind]
    media = media_cls(media=content["file_id"], caption=content["text"],
                      caption_entities=entities(content, custom_emoji), parse_mode=None,
                      show_caption_above_media=content["above"], has_spoiler=content["spoiler"])
    return await bot.edit_message_media(**common, media=media)


def utf16_length(value):
    """Telegram entity offsets are measured in UTF-16 code units."""
    return len(value.encode("utf-16-le")) // 2


def source_text_with_link(content, url):
    """Prepare linked caption without changing existing custom emoji ranges."""
    kind = content["kind"]
    if kind == "sticker":
        raise ValueError("К стикеру нельзя добавить текстовую ссылку. Для одного поста используй фото, видео, GIF или текст.")
    original = content.get("text") or ""
    suffix = "\n\n📥 Скачать файл"
    combined = original + suffix
    limit = 4096 if kind == "text" else 1024
    if utf16_length(combined) > limit:
        raise ValueError(f"Описание слишком длинное для ссылки «Скачать файл» (предел Telegram: {limit} символов UTF-16).")
    positions = entities(content, custom_emoji=True)
    positions.append(MessageEntity(type="text_link", offset=utf16_length(original + "\n\n"),
                                   length=utf16_length("📥 Скачать файл"), url=url))
    return combined, positions


async def prepare_forward_source(bot, content, url):
    """Edit only the original preview in the owner's chat, never the forwarded post.

    If the original can't be edited, stop rather than silently downgrade to
    copyMessage/sendMessage, which loses Premium emoji.
    """
    source = content.get("emoji_source")
    if not isinstance(source, dict) or type(source.get("chat_id")) is not int or type(source.get("message_id")) is not int:
        raise ValueError("Оригинал поста для пересылки не найден. Открой редактирование поста и сохрани новый предпросмотр.")
    rendered, rendered_entities = source_text_with_link(content, url)
    try:
        if content["kind"] == "text":
            await bot.edit_message_text(chat_id=source["chat_id"], message_id=source["message_id"],
                                        text=rendered, entities=rendered_entities, parse_mode=None,
                                        reply_markup=None)
        else:
            await bot.edit_message_caption(chat_id=source["chat_id"], message_id=source["message_id"],
                                           caption=rendered, caption_entities=rendered_entities,
                                           parse_mode=None, reply_markup=None)
    except TelegramAPIError as exc:
        if "message is not modified" not in str(exc).lower():
            raise ValueError("Telegram не позволил подготовить оригинал с Premium-эмодзи и ссылкой. "
                             "Пост не опубликован, чтобы не потерять эмодзи: " + str(exc)) from None
    return source


async def publish_single_content(bot, chat_id, content, keyboard=None):
    """One forwarded post; download link is a text_link in the source caption."""
    source = content.get("emoji_source")
    if not isinstance(source, dict) or type(source.get("chat_id")) is not int or type(source.get("message_id")) is not int:
        raise ValueError("Нет сообщения-оригинала для пересылки.")
    if source["chat_id"] == chat_id:
        raise ValueError("Исходное сообщение должно находиться вне канала назначения.")
    try:
        return await bot.forward_message(chat_id=chat_id, from_chat_id=source["chat_id"],
                                         message_id=source["message_id"])
    except TelegramAPIError as exc:
        raise ValueError("Telegram не разрешил переслать оригинал: " + str(exc)) from None
