"""Проверки подписок, выдача файлов и синхронизация публикаций."""
import asyncio
import logging
import secrets
import time
from collections import OrderedDict

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from content import has_custom_emoji, send_content, url_keyboard, valid_url
from api import Channels, explain_error, as_dict, private_recipient_unavailable

log = logging.getLogger(__name__)


def is_subscriber(member):
    member = as_dict(member)
    return (member.get("status") in ("creator", "administrator", "member")
            or (member.get("status") == "restricted" and member.get("is_member") is True))


class Service:
    def __init__(self, bot, db, settings, username):
        self.bot, self.db, self.settings, self.username = bot, db, settings, username
        self.channels = Channels(bot)
        self.publish_lock = asyncio.Lock()
        self.recent = OrderedDict()
        self.alert_times = {}

    def channel_keyboard(self, url):
        """Единственная URL-кнопка — Telegram переносит её при forwardMessage."""
        return url_keyboard("Установить", url)

    async def notify_admins(self, text, category=None):
        now = time.monotonic()
        if category and now - self.alert_times.get(category, -1000) < 300:
            return
        if category:
            self.alert_times[category] = now
        for admin in self.settings.admin_ids:
            try:
                await self.bot.send_message(admin, text[:4000])
            except TelegramAPIError as exc:
                if private_recipient_unavailable(exc):
                    log.info("Уведомление администратору пропущено: личный чат недоступен.")
                else:
                    log.warning("Не удалось отправить администратору уведомление: %s", explain_error(exc))

    async def preview(self, chat_id, key, subscription_checked=False):
        if not subscription_checked and not await self.require_subscription(chat_id, {"kind": "preview", "key": key}):
            return
        post = self.db.post(key, active=True)
        if not post or not self.db.file(post["file_key"], active=True):
            await self.bot.send_message(chat_id, "Пост не найден или удалён.")
            return
        # После перехода из канала показываем пример файла В ЛИЧНОМ ЧАТЕ.
        # Инлайн-кнопка доступна здесь, а не в пересланном сообщении канала.
        # Внешняя ссылка должна проходить через сократитель и возвращать
        # пользователя на /start <file_key>, который выдаёт документ.
        if not post["external_url"]:
            await self.bot.send_message(chat_id, "Ссылка для скачивания ещё не настроена.")
            return
        await send_content(self.bot, chat_id, post["content"],
                           url_keyboard("📥 Скачать", post["external_url"]))

    async def missing_subscriptions(self, user_id):
        # Каждое действие — новая проверка реальной подписки. Запросы к разным
        # спонсорам выполняем параллельно (не более трёх за раз).
        sponsors = self.db.sponsors()
        semaphore = asyncio.Semaphore(3)

        async def check(sponsor):
            async with semaphore:
                try:
                    own = await self.channels.member(sponsor["chat_id"], self.bot.id)
                    if own["status"] not in ("administrator", "creator"):
                        return "error", {**sponsor, "reason": "Бот не администратор этого канала."}
                    member = await self.channels.member(sponsor["chat_id"], user_id)
                    if not is_subscriber(member):
                        return "missing", sponsor
                    return "ok", sponsor
                except (TelegramAPIError, ValueError) as exc:
                    log.warning("Проверка спонсора %s: %s", sponsor["chat_id"], explain_error(exc))
                    return "error", {**sponsor, "reason": explain_error(exc)}

        results = await asyncio.gather(*(check(sponsor) for sponsor in sponsors))
        return ([item for status, item in results if status == "missing"],
                [item for status, item in results if status == "error"])

    async def require_subscription(self, user_id, action):
        missing, errors = await self.missing_subscriptions(user_id)
        if not missing and not errors:
            self.db.execute("DELETE FROM pending_actions WHERE user_id=?", (user_id,))
            return True
        token = secrets.token_hex(6)
        self.db.set_pending(user_id, token, action)
        rows = [[InlineKeyboardButton(text=s["title"][:64], url=s["url"])] for s in missing + errors]
        rows.append([InlineKeyboardButton(text="Проверить подписку", callback_data=f"sub:{token}")])
        text = ("Для продолжения подпишитесь на указанные каналы, затем нажмите «Проверить подписку»."
                if not errors else "Некоторые подписки сейчас не удалось проверить. Повторите попытку позднее. Администратор уведомлён.")
        await self.bot.send_message(user_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        if errors:
            await self.notify_admins("Не работает проверка подписок:\n" + "\n".join(
                f"{s['title']} ({s['chat_id']}): {s['reason']}" for s in errors), category="sponsor_check")
        return False

    async def deliver(self, user_id, key, subscription_checked=False):
        if not subscription_checked and not await self.require_subscription(user_id, {"kind": "download", "key": key}):
            return
        now = time.monotonic()
        previous = self.recent.get(user_id)
        if previous is not None and now - previous < self.settings.download_cooldown_seconds:
            await self.bot.send_message(user_id, "Подождите несколько секунд перед повторным скачиванием.")
            return
        self.recent[user_id] = now
        self.recent.move_to_end(user_id)
        while len(self.recent) > 10000:
            self.recent.popitem(last=False)
        file = self.db.file(key, active=True)
        if not file:
            await self.bot.send_message(user_id, "Файл недоступен: он удалён или не найден.")
            return
        try:
            await self.bot.send_document(user_id, file["file_id"],
                                         caption=file.get("delivery_text") or None,
                                         parse_mode=None)
        except TelegramRetryAfter as exc:
            await self.bot.send_message(user_id, f"Telegram просит подождать {exc.retry_after} сек. Повтори переход позже.")
            return
        except TelegramAPIError as exc:
            if private_recipient_unavailable(exc):
                raise
            await self.bot.send_message(user_id, "Не удалось отправить файл. Администратор уведомлён.")
            await self.notify_admins(f"Ошибка выдачи файла ID {key}: {explain_error(exc)}",
                                     category=f"file:{key}")
            return
        self.db.execute("INSERT INTO downloads(post_id,user_id) VALUES(?,?)", (key, user_id))

    async def upload_file(self, message):
        if not message.document or message.media_group_id:
            raise ValueError("Отправь один файл как документ, без альбома.")
        async with self.publish_lock:
            previous = self.db.one("SELECT file_key FROM upload_inputs WHERE chat_id=? AND message_id=?",
                                    (message.chat.id, message.message_id))
            if previous:
                file = self.db.file(previous["file_key"])
                if file["status"] == "active":
                    return file
                raise ValueError(f"Загрузка ID {file['id']} уже обрабатывалась. Результат не подтверждён или файл удалён. "
                                 "Проверь канал или чат-хранилище; автоматического повтора отправки нет.")
            chat, own = await self.channels.inspect(self.settings.storage_channel_id, "storage")
            file, fresh = self.db.reserve_upload(message.chat.id, message.message_id,
                                                 message.document.file_id, message.document.file_name or "Документ")
            try:
                from aiogram.types import MessageEntity
                stored = await self.bot.send_document(chat["id"], message.document.file_id,
                    caption=f"id: {file['id']}\nФайл: {file['file_name'][:180]}",
                    caption_entities=[MessageEntity(type="code", offset=4, length=len(file['id']))],
                    parse_mode=None)
            except TelegramAPIError as exc:
                self.db.execute("UPDATE files SET status='failed' WHERE id=?", (file["id"],))
                raise ValueError(f"Загрузка ID {file['id']} не подтверждена: {explain_error(exc)} "
                                 "Перед повторной отправкой проверь хранилище файлов.") from None
            stored_file_id = stored.document.file_id if stored.document else message.document.file_id
            self.db.finish_upload(file["id"], stored_file_id, stored.chat.id, stored.message_id)
            return self.db.file(file["id"], active=True)

    async def delete_file(self, key):
        file = self.db.file(key)
        if not file:
            raise ValueError("Файл не найден.")
        # Отзываем ID до запросов Telegram, чтобы ошибка удаления не оставила выдачу включённой.
        self.db.execute("UPDATE files SET status='deleted' WHERE id=?", (key,))
        notes = [f"Файл ID {key} удалён из выдачи. Старая ссылка отключена."]
        for linked_post in self.db.posts_for_file(key):
            notes.append(await self.delete(linked_post["id"], keep_file_note=False))
        if file["source_message"] > 0 and not file["storage_removed"]:
            try:
                await self.bot.delete_message(file["source_chat"], file["source_message"])
                self.db.execute("UPDATE files SET storage_removed=1 WHERE id=?", (key,))
                notes.append("Документ удалён из хранилища.")
            except TelegramBadRequest as exc:
                if "message to delete not found" in exc.message.lower():
                    self.db.execute("UPDATE files SET storage_removed=1 WHERE id=?", (key,))
                    notes.append("Документа уже нет в хранилище.")
                else:
                    notes.append("Telegram не разрешил удалить документ из канала (например, прошло 48 часов). "
                                 "Удали его вручную; выдача уже отключена. " + explain_error(exc))
            except TelegramAPIError as exc:
                notes.append("Очистка хранилища не завершена: " + explain_error(exc))
        return "\n".join(notes)

    @staticmethod
    def _url_button_present(message, url):
        """Пересылка сохраняет только допустимые URL-клавиатуры.

        Проверяем результат сервера: отсутствие кнопки — ошибка, а не успех.
        """
        markup = getattr(message, "reply_markup", None)
        if markup is None:
            return False
        for row in getattr(markup, "inline_keyboard", []):
            for button in row:
                if getattr(button, "url", None) == url:
                    return True
        return False

    async def _forward_with_inline_button(self, channel_id, content, short_url):
        """Создать BOT-сообщение с Premium-эмодзи в ЛС владельца,
        затем переслать в канал ВМЕСТЕ с исходной URL-кнопкой.

        Bot API changelog, 2019-05-31: URL-клавиатуры сохраняются
        при пересылке. Бот не пытается редактировать пересланный пост.
        """
        admin_id = next(iter(self.settings.admin_ids), None)
        if admin_id is None:
            raise ValueError("В настройках не задан администратор для подготовки поста.")
        keyboard = self.channel_keyboard(short_url)
        # В V10-V16 emoji_source иногда указывал на фотографию БЕЗ текста,
        # который администратор вводил следующим шагом. Поэтому не копируем
        # старую подпись, а создаём точный текущий пост с его UTF-16 entities.
        exact_content = {k: v for k, v in content.items()
                         if k not in ("emoji_source", "prepared_source", "forward_preview")}
        source = None
        posted = None
        try:
            try:
                source = await send_content(self.bot, admin_id, exact_content,
                                            keyboard, custom_emoji=True)
            except TelegramAPIError as exc:
                raise ValueError("Не удалось создать исходный пост с кнопкой в личном чате администратора. "
                                 "Для custom_emoji бот должен иметь право отправлять Premium-эмодзи "
                                 "в этот личный чат: " + explain_error(exc)) from None
            if not self._url_button_present(source, short_url):
                raise ValueError("Telegram не прикрепил кнопку к исходному сообщению. "
                                 "Пересылка отменена — пост в канале не создан.")
            if has_custom_emoji(exact_content):
                source_entities = (getattr(source, "caption_entities", None) or
                                   getattr(source, "entities", None) or [])
                expected_ids = {
                    str(entity["custom_emoji_id"])
                    for entity in exact_content.get("entities", [])
                    if entity.get("type") == "custom_emoji"
                }
                actual_ids = {
                    str(getattr(entity, "custom_emoji_id", None))
                    for entity in source_entities
                    if getattr(entity, "type", None) == "custom_emoji"
                }
                if not expected_ids.issubset(actual_ids):
                    raise ValueError("Telegram не сохранил Premium-эмодзи в исходном сообщении. "
                                     "Пересылка остановлена, чтобы не публиковать пост без них. "
                                     "Проверь Premium у владельца бота и оформление описания.")
            try:
                posted = await self.bot.forward_message(
                    chat_id=channel_id, from_chat_id=admin_id,
                    message_id=source.message_id)
            except TelegramAPIError as exc:
                raise ValueError("Telegram отказал в пересылке поста: "
                                 + explain_error(exc)) from None
            if not self._url_button_present(posted, short_url):
                # Telegram создал пост, но кнопка не перенеслась. Удаляем
                # неудачную публикацию, вместо молчаливой потери кнопки.
                try:
                    await self.bot.delete_message(channel_id, posted.message_id)
                except TelegramAPIError as exc:
                    log.error("В канале остался пост без кнопки (%s/%s): %s",
                              channel_id, posted.message_id, explain_error(exc))
                    raise ValueError(
                        "Пересылка состоялась без инлайн-кнопки. Удаление не удалось! "
                        f"Проверь сообщение {posted.message_id} в канале: "
                        + explain_error(exc)) from None
                raise ValueError("Telegram не перенёс URL-кнопку при пересылке. "
                                 "Пост без кнопки удалён из канала.")
            if has_custom_emoji(exact_content):
                forwarded_entities = (getattr(posted, "caption_entities", None) or
                                      getattr(posted, "entities", None) or [])
                expected_ids = {
                    str(e["custom_emoji_id"]) for e in exact_content.get("entities", [])
                    if e.get("type") == "custom_emoji"
                }
                actual_ids = {
                    str(getattr(e, "custom_emoji_id", None)) for e in forwarded_entities
                    if getattr(e, "type", None) == "custom_emoji"
                }
                if not expected_ids.issubset(actual_ids):
                    try:
                        await self.bot.delete_message(channel_id, posted.message_id)
                    except TelegramAPIError as exc:
                        log.error("Не удалось удалить пост без Premium-эмодзи %s: %s",
                                  posted.message_id, explain_error(exc))
                        raise ValueError(
                            "Telegram убрал Premium-эмодзи при пересылке. "
                            "Не удалось удалить опубликованный пост, проверь канал: "
                            + explain_error(exc)) from None
                    raise ValueError("Telegram убрал Premium-эмодзи при пересылке. "
                                     "Публикация без них удалена из канала.")
            return posted
        finally:
            # Временный предпросмотр не засоряет админский личный чат.
            # Для новой публикации создаём новый источник.
            if source is not None:
                try:
                    await self.bot.delete_message(admin_id, source.message_id)
                except TelegramAPIError as exc:
                    log.warning("Не удалось убрать временный предпросмотр %s: %s",
                                source.message_id, explain_error(exc))

    async def publish(self, key):
        """Одна пересылка с Premium-эмодзи и URL-кнопкой сразу на сократитель."""
        post = self.db.post(key)
        if not post or post["status"] not in ("draft", "published"):
            raise ValueError("Этот пост удалён или не найден.")
        if not self.db.file(post["file_key"], active=True):
            raise ValueError("Файл удалён или ещё не загружен. Публикация недоступна.")
        try:
            url = valid_url(post["external_url"])
        except ValueError:
            raise ValueError("Укажи сокращённую ссылку для кнопки «Установить».") from None
        await self.channels.inspect(post["channel_id"], "main")
        message = await self._forward_with_inline_button(post["channel_id"],
                                                          post["content"], url)
        self.db.record_publication(key, post["channel_id"], message.message_id, None)
        return message.message_id

    async def update(self, key, url, content, revision):
        """Пересланное сообщение не редактируется; заменяем каждую копию."""
        post = self.db.post(key)
        if not post or post["status"] == "deleted" or post["revision"] != revision:
            raise ValueError("Пост изменился или удалён. Открой его заново из списка.")
        if post["status"] == "draft":
            self.db.update_post(key, url, content)
            return
        url = valid_url(url)
        publications = self.db.publications(key)
        if not publications:
            raise ValueError("Не найдены активные копии поста. Проверь канал.")
        failed_deletions = []
        for publication in publications:
            chat_id = publication["channel_id"]
            new_message = await self._forward_with_inline_button(chat_id, content, url)
            # Сначала учёт новой публикации: она уже есть в канале.
            self.db.replace_publication(publication["id"], key, chat_id,
                                        new_message.message_id, None)
            for old_id in (publication.get("button_message_id"), publication["message_id"]):
                if old_id:
                    try:
                        await self.bot.delete_message(chat_id, old_id)
                    except TelegramAPIError as exc:
                        failed_deletions.append((chat_id, old_id, explain_error(exc)))
        self.db.update_post(key, url, content)
        if failed_deletions:
            details = ", ".join(f"{cid}/{mid}" for cid, mid, _ in failed_deletions[:5])
            raise ValueError("Новые посты опубликованы, но часть старых сообщений не удалось "
                             f"удалить ({details}). Проверь канал вручную.")

    async def delete(self, key, keep_file_note=True):
        post = self.db.post(key)
        if not post:
            raise ValueError("Пост не найден.")
        # Отключаем только этот пост и его копии; сам файл и другие посты
        # остаются доступны. При удалении файла отдельный метод отзывает ID.
        self.db.execute("UPDATE posts SET status='deleted',revision=revision+1 WHERE id=?", (key,))
        copies = self.db.publications(key, include_removed=True)
        if not copies:
            self.db.execute("UPDATE posts SET message_id=NULL WHERE id=?", (key,))
            return "Черновик удалён. Сам файл и другие посты не затронуты."
        deleted = failures = 0
        for copy in copies:
            if copy["removal_status"] == "deleted":
                continue
            successful = True
            # Remove the companion link first to disable downloads from the post.
            for message_id in (copy["button_message_id"], copy["message_id"]):
                if not message_id:
                    continue
                try:
                    await self.bot.delete_message(copy["channel_id"], message_id)
                except TelegramBadRequest as exc:
                    if "message to delete not found" not in exc.message.lower():
                        successful = False
                        log.warning("Не удалось удалить сообщение %s: %s", message_id, explain_error(exc))
                except TelegramAPIError as exc:
                    successful = False
                    log.warning("Не удалось удалить сообщение %s: %s", message_id, explain_error(exc))
            self.db.mark_publication_removed(copy["id"], "deleted" if successful else "failed")
            if successful:
                deleted += 1
            else:
                failures += 1
        self.db.execute("UPDATE posts SET message_id=NULL WHERE id=?", (key,))
        note = f"Публикация отключена. Удалено комплектов пост + кнопка: {deleted}."
        if failures:
            note += f" Не удалось полностью удалить {failures} комплектов; проверь канал вручную."

        if keep_file_note:
            note += " Файл в хранилище сохранён."
        return note
