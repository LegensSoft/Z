"""Все пользовательские действия и русская админка /admin."""
import asyncio
import logging
import secrets
import sqlite3

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.filters.command import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message, MessageOriginChannel, MessageOriginChat)

from content import deep_link, has_custom_emoji, save_emoji_source, send_content, snapshot, valid_id, valid_url
from advertising import AudienceRegistry, AdsBroadcaster, ad_keyboard, send_ad, validate_ad
from api import explain_error
from settings import channel_ref
from access import SubscriptionGate
from delivery import PrivateChatGuard
from ui import panel, chat_id_of

log = logging.getLogger(__name__)


class PostFlow(StatesGroup):
    url = State()
    key = State()
    body = State()
    description = State()
    confirm = State()


class AdFlow(StatesGroup):
    media = State()
    text = State()
    buttons = State()
    confirm = State()
    title = State()


class SponsorFlow(StatesGroup):
    channel = State()
    link = State()


class FileFlow(StatesGroup):
    document = State()
    delivery = State()


class TemplateFlow(StatesGroup):
    name = State()
    body = State()
    rename = State()


class ChannelFlow(StatesGroup):
    target = State()


def keyboard(*rows):
    """Кнопки в формате (подпись, callback_data)."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=title, callback_data=data) for title, data in row
    ] for row in rows])


def admin_keyboard():
    return keyboard([("Создать пост с нуля", "a:new"), ("Опубликовать", "a:drafts:0")],
                    [("Загрузить файл", "a:upload"), ("Посты · Редактировать", "a:posts:0")],
                    [("Реклама", "a:ads"), ("Спонсоры", "a:sponsors")],
                    [("Заготовки выдачи", "a:templates")],
                    [("Файлы", "a:files:0"), ("Диагностика", "a:diagnostics")],
                    [("Моя подписка", "a:mysub")],
                    [("Каналы", "a:channels")],
                    [("Продолжить черновик", "a:resume")])


def cancel_keyboard():
    return keyboard([("Отмена", "a:cancel")])


class AdminInputCleanup(BaseMiddleware):
    """Стираем обработанный ввод владельца уже ПОСЛЕ ответа обработчика.

    Не касается сообщений других пользователей, каналов и предпросмотров.
    Если обработка упала с исключением, входное сообщение остаётся в чате.
    """

    async def __call__(self, handler, event, data):
        result = await handler(event, data)
        if isinstance(event, Message) and event.chat.type == "private":
            try:
                await event.delete()
            except (TelegramAPIError, AttributeError, TypeError) as exc:
                log.debug("Не удалось очистить обработанный ввод: %s", exc)
        return result


class AdminGuard(BaseMiddleware):
    def __init__(self, admin_ids):
        self.admin_ids = admin_ids

    async def __call__(self, handler, event, data):
        user = event.from_user
        message = event.message if isinstance(event, CallbackQuery) else event
        if not user or user.id not in self.admin_ids or not message or message.chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Эта кнопка недоступна.")
            return
        return await handler(event, data)


def build_routers(service):
    bot, db, cfg = service.bot, service.db, service.settings
    ads = AdsBroadcaster(bot, db, cfg.admin_ids)
    service.ads = ads
    root = Router(name="filebot_v10")
    delivery_guard = PrivateChatGuard()
    root.message.outer_middleware(AudienceRegistry(db))
    root.callback_query.outer_middleware(AudienceRegistry(db))
    root.message.outer_middleware(delivery_guard)
    root.callback_query.outer_middleware(delivery_guard)
    root.message.outer_middleware(SubscriptionGate(service))
    root.callback_query.outer_middleware(SubscriptionGate(service))
    public, admin, storage = Router(name="public"), Router(name="admin"), Router(name="storage")
    public.message.filter(F.chat.type == "private")
    public.callback_query.filter(F.message.chat.type == "private")
    admin.message.filter(F.chat.type == "private")
    admin.callback_query.filter(F.message.chat.type == "private")
    admin.message.outer_middleware(AdminGuard(cfg.admin_ids))
    admin.callback_query.outer_middleware(AdminGuard(cfg.admin_ids))
    # Inner middleware запускается только при совпадении обработчика.
    admin.message.middleware(AdminInputCleanup())

    def storage_chat(chat):
        ref = cfg.storage_channel_id
        return chat.id == ref or (isinstance(ref, str) and chat.username and
                                   ref.casefold() == ("@" + chat.username).casefold())

    @storage.edited_channel_post(lambda m: storage_chat(m.chat), F.document)
    @storage.edited_message(lambda m: m.chat.type in ("group", "supergroup") and storage_chat(m.chat), F.document)
    async def update_stored_file(message: Message):
        file = db.one("SELECT * FROM files WHERE source_chat=? AND source_message=? AND status='active'",
                      (message.chat.id, message.message_id))
        if file:
            db.execute("UPDATE files SET file_id=?,file_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (message.document.file_id, message.document.file_name or file["file_name"], file["id"]))

    @storage.message(Command("storage", "id"), F.chat.type.in_({"group", "supergroup"}))
    async def group_storage_command(message: Message, command: CommandObject):
        if not message.from_user or message.from_user.id not in cfg.admin_ids or message.sender_chat:
            return
        if db.admin_subscription_enabled(message.from_user.id):
            if not await service.require_subscription(message.from_user.id, {"kind": "admin"}):
                return
        if command.command == "id":
            await bot.send_message(message.from_user.id, f"Чат: {message.chat.title}\nID хранилища: {message.chat.id}")
            return
        try:
            chat, own = await service.channels.inspect(message.chat.id, "storage")
            if db.one("SELECT chat_id FROM sponsors WHERE chat_id=?", (chat["id"],)):
                raise ValueError("Этот чат используется спонсором. Сначала убери его из спонсоров.")
            cfg.save_channels(storage=chat["id"])
            await bot.send_message(message.from_user.id,
                f"Хранилище файлов привязано: {chat.get('title','')}\nID: {chat['id']}\nМожно загружать файлы в личный чат бота.")
        except (ValueError, TelegramAPIError) as exc:
            await bot.send_message(message.from_user.id, friendly_error(exc))
        except OSError:
            await bot.send_message(message.from_user.id, "Не удалось сохранить настройки. Проверь доступ Pydroid к папке проекта.")

    @public.message(Command("id"))
    async def show_id(message: Message):
        text = f"Твой Telegram ID: {message.from_user.id}"
        if isinstance(message.forward_origin, MessageOriginChannel):
            text += f"\nID канала: {message.forward_origin.chat.id}"
        await panel(message, text)

    @public.message(CommandStart())
    async def start(message: Message, command: CommandObject, subscription_checked=False):
        payload = command.args or ""
        if not payload:
            text = "Приветствую в файловом боте канала @SimpleDLC\nВыберете файл в канале, чтобы его скачать"
            await panel(message, text)
            return
        preview = payload.startswith("p_")
        key = payload[2:] if preview else payload
        try:
            valid_id(key)
        except ValueError:
            await panel(message, "Некорректная ссылка на файл.")
            return
        if preview:
            await service.preview(message.chat.id, key, subscription_checked=subscription_checked)
        else:
            await service.deliver(message.from_user.id, key, subscription_checked=subscription_checked)

    @public.callback_query(F.data.startswith("check:"))
    async def recheck(callback: CallbackQuery, subscription_checked=False):
        await callback.answer()
        key = callback.data.partition(":")[2]
        try:
            valid_id(key)
        except ValueError:
            await panel(callback, "Некорректный ID.")
            return
        await service.deliver(callback.from_user.id, key, subscription_checked=subscription_checked)

    @public.callback_query(F.data.startswith("sub:"))
    async def subscription_resume(callback: CallbackQuery):
        await callback.answer()
        pending = db.pending(callback.from_user.id)
        if not pending or pending["token"] != callback.data.partition(":")[2]:
            await panel(callback, "Эта проверка устарела. Повтори переход из поста или отправь /start.")
            return
        action = pending["action"]
        if not await service.require_subscription(callback.from_user.id, action):
            return
        if action["kind"] == "preview":
            await service.preview(callback.from_user.id, action["key"], subscription_checked=True)
        elif action["kind"] == "download":
            await service.deliver(callback.from_user.id, action["key"], subscription_checked=True)
        elif action["kind"] == "id":
            await panel(callback, f"Твой Telegram ID: {callback.from_user.id}")
        elif action["kind"] == "admin" and callback.from_user.id in cfg.admin_ids:
            await panel(callback, "Подписка подтверждена. Можешь продолжить управление ботом.",
                                          reply_markup=admin_keyboard())
        else:
            await panel(callback, "Приветствую в файловом боте канала @SimpleDLC\n"
                                          "Выберете файл в канале, чтобы его скачать")

    async def my_subscription_status(message):
        enabled = db.admin_subscription_enabled(next(iter(cfg.admin_ids)))
        await panel(message, "Проверка подписки для твоего аккаунта: "
                             + ("включена." if enabled else "выключена.")
                             + "\nЭта настройка не влияет на остальных пользователей.",
                             reply_markup=keyboard([("Выключить" if enabled else "Включить", "a:togglemycheck")],
                                                   [("Меню", "a:menu")]))

    @admin.message(Command("mysub"))
    async def own_subscription_command(message: Message):
        await my_subscription_status(message)

    @admin.callback_query(F.data == "a:mysub")
    async def own_subscription_menu(callback: CallbackQuery):
        await callback.answer()
        await my_subscription_status(callback)

    @admin.callback_query(F.data == "a:togglemycheck")
    async def own_subscription_toggle(callback: CallbackQuery):
        enabled = not db.admin_subscription_enabled(callback.from_user.id)
        db.set_admin_subscription_enabled(callback.from_user.id, enabled)
        await callback.answer("Настройка сохранена")
        await my_subscription_status(callback)

    async def cleanup_wizard_preview(state, chat_id, keep_premium_source=False):
        data = await state.get_data()
        preview_id = data.get("preview_message_id")
        # Keep a genuinely premium-emoji preview while its published/draft post
        # references it; a fallback preview without premium is safe to remove.
        source = (data.get("content") or {}).get("emoji_source") or {}
        if (keep_premium_source and has_custom_emoji(data.get("content") or {})
                and source.get("chat_id") == chat_id
                and source.get("message_id") == preview_id):
            return
        if preview_id:
            try:
                await bot.delete_message(chat_id, preview_id)
            except TelegramAPIError:
                pass

    @admin.message(Command("admin"))
    async def admin_menu(message: Message):
        await panel(message, "Управление ботом", reply_markup=admin_keyboard())

    @admin.message(Command("cancel"))
    async def cancel_command(message: Message, state: FSMContext):
        await cleanup_wizard_preview(state, message.chat.id)
        await state.clear()
        await panel(message, "Текущее действие отменено.", reply_markup=admin_keyboard())

    @admin.callback_query(F.data == "a:cancel")
    async def cancel_callback(callback: CallbackQuery, state: FSMContext):
        await cleanup_wizard_preview(state, callback.message.chat.id)
        await state.clear()
        await callback.answer("Отменено")
        await panel(callback, "Управление ботом", reply_markup=admin_keyboard())

    async def resume_to(message, state):
        current = await state.get_state()
        if not current:
            await panel(message, "Нет незаконченного действия.", reply_markup=admin_keyboard())
            return
        if current == AdFlow.confirm.state:
            await ad_confirm(message, state)
            return
        if current == PostFlow.confirm.state:
            await show_preview(message, state)
            return
        if current == PostFlow.key.state:
            await choose_file(message)
            return
        prompts = {
            AdFlow.media.state: "Отправь фото, видео, GIF, стикер или выбери текст без медиа.",
            AdFlow.text.state: "Отправь текст рекламы. Поддерживается Telegram-оформление и premium-эмодзи.",
            AdFlow.buttons.state: "Введи инлайн-кнопки: одна строка — Название | https://ссылка. Или выбери «Без кнопок».",
            AdFlow.title.state: "Напиши название рекламы для админки.",
            AdFlow.confirm.state: "Реклама готова. Сохрани, посмотри предпросмотр или измени данные.",
            PostFlow.url.state: "Пришли внешнюю ссылку для кнопки «Установить». Можно сохранить черновик без ссылки.",
            PostFlow.key.state: "Выбери загруженный файл или отправь документ сюда — бот сам назначит ID.",
            PostFlow.body.state: "Пришли фото, видео, GIF или текст поста. Описание для медиа отправляется следующим сообщением.",
            PostFlow.description.state: "Теперь пришли описание отдельным текстовым сообщением. Форматирование и эмодзи сохраняются.",
            SponsorFlow.channel.state: "Пришли @username канала или его числовой ID -100…",
            SponsorFlow.link.state: "Пришли действующую ссылку приглашения в приватный канал.",
            FileFlow.document.state: "Отправь файл как документ. Бот сам создаст буквенный ID и сохранит документ в хранилище.",
            FileFlow.delivery.state: "Напиши текст, который увидит пользователь при выдаче файла, или выбери заготовку ниже.",
            TemplateFlow.name.state: "Пришли название новой заготовки (до 60 символов).",
            TemplateFlow.body.state: "Пришли текст заготовки для выдачи файла (до 1024 символов).",
            TemplateFlow.rename.state: "Пришли новое название заготовки.",
            ChannelFlow.target.state: "Перешли пост из нужного канала либо пришли @username, ID -100… или ссылку на пост.",
        }
        if current == FileFlow.delivery.state:
            await prompt_delivery(message, state)
            return
        if current == PostFlow.url.state:
            data = await state.get_data()
            rows = [[("Сохранить черновик без ссылки", "a:savewithouturl")]] if data.get("mode") == "new" else []
            rows.append([("Отмена", "a:cancel")])
            await panel(message, prompts[current], reply_markup=keyboard(*rows))
            return
        await panel(message, prompts.get(current, "Отмени действие: /cancel"), reply_markup=cancel_keyboard())

    @admin.message(Command("resume"))
    async def resume_command(message: Message, state: FSMContext):
        await resume_to(message, state)

    @admin.callback_query(F.data == "a:resume")
    async def resume_callback(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await resume_to(callback, state)

    @admin.callback_query((F.data == "a:new") | F.data.startswith("a:newfor:"))
    async def new_post(callback: CallbackQuery, state: FSMContext):
        if not cfg.main_channel_id or not cfg.storage_channel_id:
            await callback.answer("Сначала привяжи основной канал и хранилище файлов через «Каналы».", show_alert=True)
            return
        await cleanup_wizard_preview(state, callback.message.chat.id)
        await state.clear()
        data = {"mode": "new"}
        if callback.data.startswith("a:newfor:"):
            key = callback.data.split(":", 2)[2]
            if not db.file(key, active=True):
                await callback.answer("Файл недоступен или удалён.", show_alert=True)
                return
            data["key"] = key
        await state.set_data(data)
        await state.set_state(PostFlow.body)
        await callback.answer()
        await panel(callback, "Создание публикации для @SimpleDLC. Сначала отправь фото, видео или GIF. "
                                      "Следующим сообщением я попрошу описание. "
                                      "Если нужен пост без картинки, отправь только текст.",
                                      reply_markup=cancel_keyboard())

    @admin.message(PostFlow.url)
    async def post_url(message: Message, state: FSMContext):
        try:
            url = valid_url(message.text or "")
        except ValueError as exc:
            await panel(message, str(exc))
            return
        await state.update_data(url=url)
        await state.set_state(PostFlow.confirm)
        await show_preview(message, state)

    async def select_file(message, state, key):
        valid_id(key)
        if not db.file(key, active=True):
            raise ValueError("Файл не найден или удалён. Загрузи документ через «Загрузить файл».")
        await state.update_data(key=key)
        await state.set_state(PostFlow.url)
        await panel(message, f"Выбран файл ID {key}.\nКонечная ссылка для сокращателя:\n"
                             f"{deep_link(service.username, key)}\n\n"
                             "Создай короткую ссылку, которая ведёт на указанную выше "
                             "конечную ссылку в бота. Затем пришли сюда адрес "
                             "сокращённой ссылки. После нажатия «Скачать» пользователь "
                             "пройдёт сократитель, вернётся в бота и получит файл. "
                             "Либо сохрани черновик и добавь ссылку позже.",
                             reply_markup=keyboard([("Сохранить черновик без ссылки", "a:savewithouturl")],
                                                   [("Отмена", "a:cancel")]))

    async def choose_file(message, offset=0):
        rows = db.all("SELECT f.id,f.file_name FROM files f "
                      "WHERE f.status='active' ORDER BY f.updated_at DESC,f.id LIMIT 9 OFFSET ?", (offset,))
        keys = [[(f"{r['id']} · {r['file_name'][:40]}", f"a:pickfile:{r['id']}")] for r in rows[:8]]
        nav = []
        if offset:
            nav.append(("Назад", f"a:pickpage:{max(0,offset-8)}"))
        if len(rows) > 8:
            nav.append(("Далее", f"a:pickpage:{offset+8}"))
        if nav:
            keys.append(nav)
        keys.append([("Отмена", "a:cancel")])
        await panel(message, "Выбери файл кнопкой, введи его ID или отправь новый документ прямо сюда. "
                             "Для нового документа ID создаётся автоматически.", reply_markup=keyboard(*keys))

    @admin.callback_query(PostFlow.url, F.data == "a:savewithouturl")
    async def save_draft_without_url(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if data.get("mode") != "new" or not data.get("key") or not data.get("content"):
            await callback.answer("Сохранение сейчас недоступно.", show_alert=True)
            return
        await callback.answer()
        try:
            async with service.publish_lock:
                post_id = db.create_post(data["key"], "", data["content"], cfg.main_channel_id)
        except ValueError as exc:
            await panel(callback, friendly_error(exc))
            return
        await cleanup_wizard_preview(state, callback.message.chat.id, keep_premium_source=True)
        await state.clear()
        await panel(callback, "Черновик сохранён без ссылки. Он появится в разделе «Опубликовать». "
                    "Открой его и выбери «Изменить ссылку», когда будешь готов.",
                    reply_markup=keyboard([("Открыть черновик", f"a:post:{post_id}")],
                                          [("Меню", "a:menu")]))

    @admin.callback_query(PostFlow.key, F.data.startswith("a:pickpage:"))
    async def pick_page(callback: CallbackQuery):
        await callback.answer()
        await choose_file(callback, max(0, int(callback.data.rsplit(":",1)[1])))

    @admin.callback_query(PostFlow.key, F.data.startswith("a:pickfile:"))
    async def pick_file(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        try:
            await select_file(callback, state, callback.data.split(":",2)[2])
        except ValueError as exc:
            await panel(callback, str(exc))

    @admin.message(PostFlow.key)
    async def post_key(message: Message, state: FSMContext):
        try:
            if message.document:
                file = await service.upload_file(message)
                key = file["id"]
                await state.update_data(key=key, after_file="post")
                await state.set_state(FileFlow.delivery)
                await panel(message, f"Файл сохранён. Его буквенный ID: {key}.")
                await prompt_delivery(message, state)
                return
            key = valid_id((message.text or "").strip())
            await select_file(message, state, key)
        except (ValueError, TelegramAPIError) as exc:
            await panel(message, friendly_error(exc))

    @admin.message(PostFlow.body)
    async def post_body(message: Message, state: FSMContext):
        try:
            content = snapshot(message)
        except ValueError as exc:
            await panel(message, str(exc))
            return
        if content["kind"] in ("photo", "video", "animation"):
            # Медиа — первый шаг; подпись к нему обязательно идёт отдельным сообщением.
            content["text"], content["entities"] = "", []
            await state.update_data(content=content)
            await state.set_state(PostFlow.description)
            await panel(message, "Фото или видео получено. Теперь пришли описание отдельным сообщением. "
                                 "Можно использовать оформление текста и premium-эмодзи.",
                                 reply_markup=keyboard([("Оставить без описания", "a:skipdesc")],
                                                       [("Отмена", "a:cancel")]))
            return
        await finish_post_body(message, state, content)

    async def finish_post_body(message, state, content):
        await state.update_data(content=content)
        data = await state.get_data()
        if data["mode"] == "edit" or data.get("url"):
            await state.set_state(PostFlow.confirm)
            await show_preview(message, state)
        elif data.get("key"):
            await select_file(message, state, data["key"])
        else:
            await state.set_state(PostFlow.key)
            await choose_file(message)

    @admin.message(PostFlow.description)
    async def post_description(message: Message, state: FSMContext):
        if message.text is None:
            await panel(message, "Для описания пришли отдельное текстовое сообщение.")
            return
        data = await state.get_data()
        content = data["content"]
        if len(message.text.encode("utf-16-le")) // 2 > 1024:
            await panel(message, "Описание слишком длинное. Максимум 1024 символа для подписи к медиа.")
            return
        content["text"] = message.text
        content["entities"] = [e.model_dump(mode="json", exclude_none=True) for e in (message.entities or [])]
        await finish_post_body(message, state, content)

    @admin.callback_query(PostFlow.description, F.data == "a:skipdesc")
    async def post_skip_description(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        content = data["content"]
        content["text"], content["entities"] = "", []
        await finish_post_body(callback, state, content)

    async def show_preview(message, state):
        data = await state.get_data()
        token = secrets.token_hex(4)
        # Изменение описания/ссылки не должно оставлять стопку старых предпросмотров.
        old_preview_id = data.get("preview_message_id")
        if old_preview_id:
            try:
                await bot.delete_message(chat_id_of(message), old_preview_id)
            except TelegramAPIError:
                pass
        url = data.get("url")
        preview_markup = (InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📥 Скачать", url=url)
        ]]) if url else None)
        preview_fallback = False
        try:
            preview = await send_content(bot, chat_id_of(message), data["content"], preview_markup)
        except TelegramBadRequest as exc:
            if not has_custom_emoji(data["content"]):
                raise
            # Previewing with custom emoji can be rejected if the bot owner
            # isn't Premium. The original entities remain intact in SQLite,
            # but a plain preview must never be used as a premium copy source.
            log.warning("Telegram refused custom emojis in private preview: %s", exc)
            preview = await send_content(bot, chat_id_of(message), data["content"],
                                         preview_markup, custom_emoji=False)
            data["content"].pop("emoji_source", None)
            preview_fallback = True
        if not preview_fallback:
            save_emoji_source(data["content"], chat_id_of(message), preview.message_id)
        await state.update_data(content=data["content"], preview_token=token,
                                preview_message_id=preview.message_id)
        label = "Сохранить изменения" if data["mode"] == "edit" else "Опубликовать"
        rows = [[("Изменить ссылку", f"a:drafturl:{token}"),
                 ("Изменить медиа", f"a:draftbody:{token}")],
                [(label, f"a:save:{token}")]]
        if data["content"]["kind"] in ("photo", "video", "animation"):
            rows.insert(1, [("Изменить описание", f"a:draftdesc:{token}")])
        if data["mode"] == "new":
            rows.append([("Сохранить черновик", f"a:savedraft:{token}")])
        rows.append([("Отмена", "a:cancel")])
        if has_custom_emoji(data["content"]):
            note = ("Telegram запретил premium-эмодзи даже в личном предпросмотре. "
                    "Он показан с обычными символами. В канале бот тоже может "
                    "получить отказ: для прямой публикации необходим дополнительный "
                    "username через Fragment."
                    if preview_fallback else
                    "Премиум-эмодзи: попробую скопировать готовый предпросмотр "
                    "без переписывания подписи. Telegram всё равно может "
                    "заменить эмодзи обычными при копировании в канал. "
                    "Служебный предпросмотр сохранится для повторов — не удаляй его.")
        else:
            note = ""
        await panel(message, f"Предпросмотр публикации\nID файла: {data['key']}\n"
                    f"Ссылка выдачи после сократителя: {deep_link(service.username, data['key'])}\n"
                    f"Сокращённая ссылка: {url or 'не задана'}\n{note}",
                    reply_markup=keyboard(*rows))

    @admin.callback_query(PostFlow.confirm, F.data.startswith("a:drafturl:") | F.data.startswith("a:draftbody:") | F.data.startswith("a:draftdesc:"))
    async def change_draft(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.data.rsplit(":", 1)[1] != data.get("preview_token"):
            await callback.answer("Предпросмотр устарел. Открой /resume.", show_alert=True)
            return
        target = (PostFlow.url if callback.data.startswith("a:drafturl:") else
                  PostFlow.description if callback.data.startswith("a:draftdesc:") else PostFlow.body)
        await state.set_state(target)
        await callback.answer()
        await resume_to(callback, state)

    @admin.callback_query(PostFlow.confirm, F.data.startswith("a:save:") | F.data.startswith("a:savedraft:"))
    async def save_post(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if callback.data.rsplit(":", 1)[1] != data.get("preview_token"):
            await callback.answer("Предпросмотр устарел. Открой /resume.", show_alert=True)
            return
        await callback.answer("Сохраняю…")
        async with service.publish_lock:
            if await state.get_state() != PostFlow.confirm.state:
                return
            data = await state.get_data()
            created_post_id = None
            try:
                if not db.file(data["key"], active=True):
                    raise ValueError("Выбранный файл удалён. Создай пост заново с другим файлом.")
                if data["mode"] == "edit":
                    post_id = data["post_id"]
                    await service.update(post_id, data["url"], data["content"], data["revision"])
                    text = ("Черновик обновлён. Опубликуй его из списка «Посты»." if db.post(post_id)["status"] == "draft"
                            else "Изменения применены в канале и в боте.")
                else:
                    # Создаём новый ID поста, а ID самого файла не меняем.
                    # В результате доступны независимые тексты/фото/ссылки для одного файла.
                    created_post_id = db.create_post(data["key"], data.get("url", ""), data["content"], cfg.main_channel_id)
                    post_id = created_post_id
                    if callback.data.startswith("a:savedraft:"):
                        text = "Черновик сохранён. Опубликовать его можно через «Опубликовать» в /admin."
                    else:
                        await service.publish(post_id)
                        text = "Отдельный пост опубликован в основном канале."
            except (TelegramAPIError, ValueError) as exc:
                if created_post_id:
                    await cleanup_wizard_preview(state, callback.message.chat.id,
                                                 keep_premium_source=True)
                    await state.clear()
                    await panel(callback, "Публикация не завершена. Новый пост сохранён как черновик. "
                                "Повтори публикацию из его карточки.\n" + friendly_error(exc),
                                reply_markup=keyboard([("Открыть черновик", f"a:post:{created_post_id}")],
                                                      [("Меню", "a:menu")]))
                else:
                    await panel(callback, friendly_error(exc) + "\nИсправь данные или используй /cancel.")
                return
            await cleanup_wizard_preview(state, callback.message.chat.id,
                                         keep_premium_source=True)
            await state.clear()
            if data["mode"] == "new" and not callback.data.startswith("a:savedraft:"):
                await panel(callback, text, reply_markup=repeat_keyboard(post_id))
            else:
                await panel(callback, text,
                            reply_markup=keyboard([("Открыть пост", f"a:post:{post_id}")],
                                                  [("Меню", "a:menu")]))

    async def list_posts(message, offset, drafts=False):
        rows = db.all("SELECT id,file_key,status FROM posts " + ("WHERE status='draft' " if drafts else "")
                      + "ORDER BY created_at DESC,id LIMIT 9 OFFSET ?", (offset,))
        labels = {"published": "Опубликован", "draft": "Черновик", "deleted": "Удалён"}
        keys = [[(f"{r['id']} · файл {r['file_key']} · {labels[r['status']]}", f"a:post:{r['id']}")] for r in rows[:8]]
        nav = []
        prefix = "a:drafts:" if drafts else "a:posts:"
        if offset:
            nav.append(("Назад", f"{prefix}{max(0, offset - 8)}"))
        if len(rows) > 8:
            nav.append(("Далее", f"{prefix}{offset + 8}"))
        if nav:
            keys.append(nav)
        keys.append([("Создать пост с нуля", "a:new"), ("Меню", "a:menu")])
        await panel(message, ("Выбери черновик для публикации" if drafts else "Публикации") if rows
                             else ("Нет черновиков. Создай пост с нуля." if drafts else "Публикаций пока нет."),
                             reply_markup=keyboard(*keys))

    @admin.callback_query(F.data == "a:menu")
    async def show_menu(callback: CallbackQuery):
        await callback.answer()
        await panel(callback, "Управление ботом", reply_markup=admin_keyboard())

    @admin.callback_query(F.data.startswith("a:posts:"))
    async def posts(callback: CallbackQuery):
        await callback.answer()
        await list_posts(callback, max(0, int(callback.data.rsplit(":", 1)[1])))

    @admin.callback_query(F.data.startswith("a:drafts:"))
    async def drafts(callback: CallbackQuery):
        await callback.answer()
        await list_posts(callback, max(0, int(callback.data.rsplit(":",1)[1])), drafts=True)

    @admin.callback_query(F.data.startswith("a:post:"))
    async def view_post(callback: CallbackQuery):
        await callback.answer()
        key = callback.data.split(":", 2)[2]
        post = db.post(key)
        if not post:
            await panel(callback, "Публикация не найдена.", reply_markup=keyboard([("К списку", "a:posts:0")]))
            return
        rows = [[("Посмотреть содержание", f"a:postpreview:{key}")]]
        if post["status"] in ("draft", "published"):
            if post["status"] == "published":
                rows.append([("Опубликовать ещё раз", f"a:republish:{key}")])
                rows.append([("3 копии подряд", f"a:repeat:{key}:3"),
                             ("5 копий подряд", f"a:repeat:{key}:5")])
            elif post["external_url"]:
                rows.append([("Опубликовать черновик", f"a:publish:{key}")])
            else:
                rows.append([("Добавить ссылку и опубликовать", f"a:editurl:{key}")])
            rows.append([("Изменить содержание", f"a:editbody:{key}")])
            if post["content"]["kind"] in ("photo", "video", "animation"):
                rows.append([("Изменить описание", f"a:editdesc:{key}")])
            rows.append([("Изменить внешнюю ссылку", f"a:editurl:{key}")])
        rows.append([("Удалить / убрать из канала", f"a:askdelete:{key}")])
        rows.append([("К публикациям", "a:posts:0"), ("Черновики", "a:drafts:0")])
        status = {"draft": "Черновик", "published": "Опубликован", "deleted": "Удалён"}.get(post["status"], post["status"])
        count = db.one("SELECT COUNT(*) AS n FROM downloads WHERE post_id=?", (post["file_key"],))["n"]
        copies = db.publications(key)
        await panel(callback, f"Публикация {key}\nID файла: {post['file_key']}\nСтатус: {status}\n"
                    f"Активных копий в канале: {len(copies)}\nВыдач этого файла: {count}\n"
                    f"Внешняя ссылка: {post['external_url'] or 'не задана'}\n"
                    f"Ссылка на файл: {deep_link(service.username, post['file_key'])}",
                    reply_markup=keyboard(*rows))

    @admin.callback_query(F.data.startswith("a:postpreview:"))
    async def post_preview(callback: CallbackQuery):
        key = callback.data.split(":", 2)[2]
        post = db.post(key)
        if not post:
            await callback.answer("Пост не найден.", show_alert=True)
            return
        await callback.answer()
        await send_content(bot, callback.message.chat.id, post["content"],
            keyboard([("Закрыть предпросмотр", "a:hidepreview")]))

    @admin.callback_query(F.data == "a:hidepreview")
    async def hide_preview(callback: CallbackQuery):
        await callback.answer()
        try:
            await callback.message.delete()
        except TelegramAPIError:
            await panel(callback, "Предпросмотр закрыт.")

    def repeat_keyboard(key):
        # Кнопки остаются на экране после публикации и пригодны для нового нажатия.
        return keyboard([("Опубликовать ещё раз", f"a:republish:{key}")],
                        [("3 копии подряд", f"a:repeat:{key}:3"),
                         ("5 копий подряд", f"a:repeat:{key}:5")],
                        [("Открыть пост", f"a:post:{key}"), ("Меню", "a:menu")])

    async def publish_copies(callback, key, count):
        # Не запускаем параллельные публикации — сохраняем порядок сообщений.
        await callback.answer("Публикую…")
        published = []
        try:
            async with service.publish_lock:
                for i in range(count):
                    if i:
                        # Telegram ограничивает частоту сообщений в один канал.
                        await asyncio.sleep(1.1)
                    published.append(await service.publish(key))
        except (TelegramAPIError, ValueError) as exc:
            result = (f"Размещено копий: {len(published)} из {count}.\n"
                      f"Последующие публикации не выполнены: {friendly_error(exc)}")
            await panel(callback, result, reply_markup=repeat_keyboard(key) if published else
                        keyboard([("Вернуться к публикации", f"a:post:{key}")]))
            return
        await panel(callback,
                    f"Пост {key} снова опубликован.\n"
                    f"Создано копий за это действие: {len(published)}.\n"
                    f"Последнее сообщение в канале: {published[-1]}.\n"
                    f"Всего активных копий: {len(db.publications(key))}.\n\n"
                    "Нажимай «Опубликовать ещё раз» сколько угодно раз — "
                    "новый файл загружать не нужно.",
                    reply_markup=repeat_keyboard(key))

    @admin.callback_query(F.data.startswith("a:republish:"))
    async def republish_post(callback: CallbackQuery):
        await publish_copies(callback, callback.data.split(":", 2)[2], 1)

    @admin.callback_query(F.data.startswith("a:repeat:"))
    async def repeat_post_batch(callback: CallbackQuery):
        try:
            _, _, key, count_text = callback.data.split(":", 3)
            count = int(count_text)
            if count not in (3, 5):
                raise ValueError
        except (ValueError, AttributeError):
            await callback.answer("Некорректное количество копий.", show_alert=True)
            return
        await publish_copies(callback, key, count)

    @admin.callback_query(F.data.startswith("a:editbody:") | F.data.startswith("a:editurl:") | F.data.startswith("a:editdesc:"))
    async def edit_post(callback: CallbackQuery, state: FSMContext):
        key = callback.data.split(":", 2)[2]
        post = db.post(key)
        if not post or post["status"] == "deleted":
            await callback.answer("Пост удалён или не найден.", show_alert=True)
            return
        await state.clear()
        await state.set_data({"mode": "edit", "key": post["file_key"], "post_id": key,
                              "url": post["external_url"], "content": post["content"],
                              "revision": post["revision"]})
        target = (PostFlow.body if callback.data.startswith("a:editbody:") else
                  PostFlow.description if callback.data.startswith("a:editdesc:") else PostFlow.url)
        await state.set_state(target)
        await callback.answer()
        await resume_to(callback, state)

    @admin.callback_query(F.data.startswith("a:publish:"))
    async def publish_draft(callback: CallbackQuery):
        await callback.answer("Публикую…")
        key = callback.data.split(":", 2)[2]
        try:
            async with service.publish_lock:
                await service.publish(key)
            await panel(callback, "Черновик опубликован в канале.", reply_markup=repeat_keyboard(key))
        except (TelegramAPIError, ValueError) as exc:
            await panel(callback, friendly_error(exc))

    @admin.callback_query(F.data.startswith("a:askdelete:"))
    async def ask_delete(callback: CallbackQuery):
        key = callback.data.split(":", 2)[2]
        await callback.answer()
        await panel(callback, f"Удалить публикацию {key} и все её копии в канале? "
                    "Другие публикации с этим файлом продолжат работать. "
                    "Если Telegram не позволит удалить старое сообщение, я попробую убрать с него кнопку. "
                    "Файл в хранилище останется.",
                                      reply_markup=keyboard([("Удалить", f"a:delete:{key}"), ("Отмена", f"a:post:{key}")]))

    @admin.callback_query(F.data.startswith("a:delete:"))
    async def delete_post(callback: CallbackQuery):
        await callback.answer("Удаляю…")
        try:
            async with service.publish_lock:
                text = await service.delete(callback.data.split(":", 2)[2])
            await panel(callback, text, reply_markup=admin_keyboard())
        except ValueError as exc:
            await panel(callback, str(exc))

    @admin.callback_query(F.data == "a:upload")
    async def upload_prompt(callback: CallbackQuery, state: FSMContext):
        if not cfg.storage_channel_id:
            await callback.answer("Сначала привяжи хранилище файлов через «Каналы».", show_alert=True)
            return
        await state.clear()
        await state.set_state(FileFlow.document)
        await callback.answer()
        await panel(callback, "Отправь файл прямо в этот чат как документ. "
                                      "Я назначу ID и сохраню файл в выбранном канале или чате.", reply_markup=cancel_keyboard())

    @admin.message(FileFlow.document)
    async def uploaded_file(message: Message, state: FSMContext):
        try:
            file = await service.upload_file(message)
        except (ValueError, TelegramAPIError) as exc:
            await panel(message, friendly_error(exc))
            return
        await state.set_data({"key": file["id"], "after_file": "receipt"})
        await state.set_state(FileFlow.delivery)
        await panel(message, f"Файл принят. Его буквенный ID: {file['id']}.")
        await prompt_delivery(message, state)

    async def upload_receipt(message, file):
        key = file["id"]
        await panel(message, f"Файл сохранён в хранилище.\nНазвание: {file['file_name'][:180]}\n"
                             f"Буквенный ID: {key}\n\nСсылка для скачивания:\n{deep_link(service.username, key)}",
                             reply_markup=keyboard([("Создать пост для файла", f"a:newfor:{key}")],
                                                     [("Файл · Удалить", f"a:file:{key}"), ("Меню", "a:menu")]))

    @admin.callback_query(F.data.startswith("a:files:"))
    async def files(callback: CallbackQuery):
        await callback.answer()
        offset = max(0, int(callback.data.rsplit(":", 1)[1]))
        rows = db.all("SELECT id,file_name,status FROM files ORDER BY updated_at DESC,id LIMIT 9 OFFSET ?", (offset,))
        labels = {"active": "", "deleted": "Удалён · ", "failed": "Не загружен · ", "uploading": "Не подтверждён · "}
        keys = [[(f"{labels.get(r['status'], '')}{r['id']} · {r['file_name'][:35]}", f"a:file:{r['id']}")] for r in rows[:8]]
        nav = []
        if offset:
            nav.append(("Назад", f"a:files:{max(0, offset - 8)}"))
        if len(rows) > 8:
            nav.append(("Далее", f"a:files:{offset + 8}"))
        if nav:
            keys.append(nav)
        keys.append([("Загрузить файл", "a:upload"), ("Меню", "a:menu")])
        await panel(callback, "Файлы" if rows else "Нет загруженных файлов.", reply_markup=keyboard(*keys))

    @admin.callback_query(F.data.startswith("a:fileposts:"))
    async def file_posts_page(callback: CallbackQuery):
        await callback.answer()
        _, _, file_key, offset_text = callback.data.split(":", 3)
        if not db.file(file_key):
            await panel(callback, "Файл не найден.", reply_markup=admin_keyboard())
            return
        try:
            offset = max(0, int(offset_text))
        except ValueError:
            offset = 0
        linked = db.posts_for_file(file_key)
        rows = [[(f"{item['id']} · {item['status']}", f"a:post:{item['id']}")]
                for item in linked[offset:offset + 8]]
        nav = []
        if offset:
            nav.append(("Назад", f"a:fileposts:{file_key}:{max(0, offset - 8)}"))
        if offset + 8 < len(linked):
            nav.append(("Далее", f"a:fileposts:{file_key}:{offset + 8}"))
        if nav:
            rows.append(nav)
        rows.append([("Создать ещё один пост", f"a:newfor:{file_key}")])
        rows.append([("К файлу", f"a:file:{file_key}")])
        await panel(callback, f"Посты файла {file_key} ({len(linked)})", reply_markup=keyboard(*rows))

    @admin.callback_query(F.data.startswith("a:file:"))
    async def file_card(callback: CallbackQuery):
        await callback.answer()
        key = callback.data.split(":",2)[2]
        file = db.file(key)
        if not file:
            await panel(callback, "Файл не найден.")
            return
        keys = []
        linked_posts = db.posts_for_file(key)
        if file["status"] == "active":
            keys.append([("Создать ещё один пост", f"a:newfor:{key}")])
        for linked in linked_posts[:8]:
            keys.append([(f"Пост {linked['id']} · {linked['status']}", f"a:post:{linked['id']}")])
        if len(linked_posts) > 8:
            keys.append([("Все посты этого файла", f"a:fileposts:{key}:0")])
        if file["status"] == "active":
            keys.append([("Изменить текст выдачи", f"a:editdelivery:{key}")])
        keys.append([("Удалить файл / завершить очистку", f"a:askfiledelete:{key}")])
        keys.append([("К файлам", "a:files:0")])
        await panel(callback, f"ID файла: {key}\nНазвание: {file['file_name'][:180]}\n"
                                      f"Статус: {file['status']}\n"
                                      f"Постов с файлом: {len(linked_posts)}\n"
                                      f"Текст выдачи: {file.get('delivery_text') or '(без подписи)'}\n"
                                      f"Ссылка: {deep_link(service.username,key)}",
                                      reply_markup=keyboard(*keys))

    def validate_delivery_text(value):
        value = (value or "").strip()
        if not value:
            raise ValueError("Пришли текст сообщения или выбери заготовку.")
        if len(value.encode("utf-16-le")) // 2 > 1024:
            raise ValueError("Текст выдачи не может превышать 1024 символа.")
        return value

    async def prompt_delivery(message, state):
        items = db.templates()
        rows = [[(f"{r['name'][:42]}", f"a:dt:{r['id']}")] for r in items[:6]]
        if len(items) > 6:
            rows.append([("Другие заготовки", "a:dtpage:6")])
        rows.append([("Без подписи", "a:dtempty"), ("Создать заготовку", "a:templateadd")])
        rows.append([("Отмена", "a:cancel")])
        await panel(message, "Напиши текст, который будет сопровождать файл при скачивании. "
                             "Либо выбери готовую заготовку. Этот текст можно изменить позже.",
                             reply_markup=keyboard(*rows))

    async def finish_delivery(message, state, value):
        data = await state.get_data()
        key = data.get("key") or data.get("return_file_key")
        if not key or not db.file(key, active=True):
            await state.clear()
            await panel(message, "Файл больше недоступен. Открой раздел «Файлы».", reply_markup=admin_keyboard())
            return
        db.update_delivery_text(key, value)
        after = data.get("after_file", "file")
        await state.clear()
        if after == "post":
            await state.set_data({k: v for k, v in data.items() if k not in ("after_file", "return_file_key")})
            await select_file(message, state, key)
        elif after == "receipt":
            await upload_receipt(message, db.file(key))
        else:
            await panel(message, f"Текст выдачи для файла {key} сохранён.",
                                 reply_markup=keyboard([("Открыть файл", f"a:file:{key}")],
                                                       [("Меню", "a:menu")]))

    @admin.callback_query(F.data.startswith("a:editdelivery:"))
    async def edit_delivery(callback: CallbackQuery, state: FSMContext):
        key = callback.data.split(":", 2)[2]
        await callback.answer()
        if not db.file(key, active=True):
            await panel(callback, "Файл не найден или удалён.")
            return
        await state.clear()
        await state.set_data({"key": key, "after_file": "file"})
        await state.set_state(FileFlow.delivery)
        await prompt_delivery(callback, state)

    @admin.message(FileFlow.delivery)
    async def enter_delivery(message: Message, state: FSMContext):
        try:
            value = validate_delivery_text(message.text)
        except ValueError as exc:
            await panel(message, str(exc))
            return
        await finish_delivery(message, state, value)

    @admin.callback_query(FileFlow.delivery, F.data == "a:dtempty")
    async def empty_delivery(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await finish_delivery(callback, state, "")

    @admin.callback_query(FileFlow.delivery, F.data.startswith("a:dtpage:"))
    async def delivery_template_page(callback: CallbackQuery):
        await callback.answer()
        offset = max(0, int(callback.data.rsplit(":", 1)[1]))
        items = db.templates()
        rows = [[(r["name"][:48], f"a:dt:{r['id']}")] for r in items[offset:offset + 6]]
        nav = []
        if offset:
            nav.append(("Назад", f"a:dtpage:{max(0, offset - 6)}"))
        if offset + 6 < len(items):
            nav.append(("Далее", f"a:dtpage:{offset+6}"))
        if nav:
            rows.append(nav)
        rows.append([("Без подписи", "a:dtempty")])
        await panel(callback, "Выбери текст выдачи:", reply_markup=keyboard(*rows))

    @admin.callback_query(FileFlow.delivery, F.data.startswith("a:dt:"))
    async def select_delivery_template(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        item = db.template(int(callback.data.rsplit(":", 1)[1]))
        if not item:
            await panel(callback, "Заготовка больше недоступна.")
            return
        await finish_delivery(callback, state, item["body"])

    @admin.callback_query((F.data == "a:templates") | F.data.startswith("a:templates:"))
    async def templates_menu(callback: CallbackQuery):
        await callback.answer()
        items = db.templates()
        offset = max(0, int(callback.data.rsplit(":", 1)[1])) if callback.data.startswith("a:templates:") else 0
        rows = [[(r["name"][:55], f"a:template:{r['id']}")] for r in items[offset:offset + 8]]
        nav = []
        if offset:
            nav.append(("Назад", f"a:templates:{max(0, offset - 8)}"))
        if offset + 8 < len(items):
            nav.append(("Далее", f"a:templates:{offset + 8}"))
        if nav:
            rows.append(nav)
        rows.append([("Создать заготовку", "a:templateadd")])
        rows.append([("Меню", "a:menu")])
        await panel(callback, "Заготовки текста для выдачи файлов. "
                                      "Здесь можно создавать, изменять и удалять готовые сообщения.",
                                      reply_markup=keyboard(*rows))

    @admin.callback_query(F.data == "a:templateadd")
    async def add_template(callback: CallbackQuery, state: FSMContext):
        is_file = await state.get_state() == FileFlow.delivery.state
        data = await state.get_data() if is_file else {}
        await state.clear()
        if is_file:
            await state.set_data({"return_file_key": data["key"], "after_file": data["after_file"],
                                  "post_data": {k:v for k,v in data.items() if k not in ("key", "after_file")}})
        else:
            await state.set_data({})
        await state.set_state(TemplateFlow.name)
        await callback.answer()
        await panel(callback, "Пришли название новой заготовки (до 60 символов).", reply_markup=cancel_keyboard())

    @admin.message(TemplateFlow.name)
    async def template_name(message: Message, state: FSMContext):
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 60:
            await panel(message, "Название должно содержать от 1 до 60 символов.")
            return
        if db.one("SELECT id FROM delivery_templates WHERE name=?", (name,)):
            await panel(message, "Такая заготовка уже есть. Пришли другое название.")
            return
        await state.update_data(template_name=name)
        await state.set_state(TemplateFlow.body)
        await panel(message, "Теперь пришли текст заготовки (до 1024 символов).", reply_markup=cancel_keyboard())

    @admin.message(TemplateFlow.body)
    async def template_body(message: Message, state: FSMContext):
        try:
            value = validate_delivery_text(message.text)
        except ValueError as exc:
            await panel(message, str(exc))
            return
        data = await state.get_data()
        try:
            if data.get("template_edit_id"):
                if not db.template(data["template_edit_id"]):
                    raise ValueError("Заготовка удалена.")
                db.execute("UPDATE delivery_templates SET body=? WHERE id=?", (value, data["template_edit_id"]))
                await state.clear()
                await panel(message, "Текст заготовки изменён.", reply_markup=admin_keyboard())
            else:
                db.execute("INSERT INTO delivery_templates(name,body) VALUES(?,?)",
                           (data["template_name"], value))
                if data.get("return_file_key"):
                    if data.get("after_file") == "post":
                        old = data.get("post_data", {})
                        await state.set_data({**old, "key": data["return_file_key"], "after_file": "post"})
                    else:
                        await state.set_data({"key": data["return_file_key"], "after_file": data["after_file"]})
                    await finish_delivery(message, state, value)
                else:
                    await state.clear()
                    await panel(message, "Заготовка сохранена. Теперь её можно выбирать при загрузке файлов.",
                                         reply_markup=admin_keyboard())
        except (ValueError, sqlite3.IntegrityError) as exc:
            await panel(message, f"Не удалось сохранить заготовку: {exc}")

    @admin.callback_query(F.data.startswith("a:template:"))
    async def template_card(callback: CallbackQuery):
        item = db.template(int(callback.data.rsplit(":", 1)[1]))
        await callback.answer()
        if not item:
            await panel(callback, "Заготовка удалена.")
            return
        key = item["id"]
        await panel(callback, f"Заготовка: {item['name']}\n\n{item['body']}",
                                      reply_markup=keyboard([("Изменить текст", f"a:templatebody:{key}")],
                                                            [("Переименовать", f"a:templaterename:{key}")],
                                                            [("Удалить", f"a:templatedelask:{key}")],
                                                            [("К списку", "a:templates")]))

    @admin.callback_query(F.data.startswith("a:templatebody:"))
    async def edit_template_body(callback: CallbackQuery, state: FSMContext):
        key = int(callback.data.rsplit(":", 1)[1])
        await callback.answer()
        if not db.template(key):
            return
        await state.clear()
        await state.set_data({"template_edit_id": key})
        await state.set_state(TemplateFlow.body)
        await panel(callback, "Пришли новый текст для этой заготовки.", reply_markup=cancel_keyboard())

    @admin.callback_query(F.data.startswith("a:templaterename:"))
    async def rename_template_prompt(callback: CallbackQuery, state: FSMContext):
        key = int(callback.data.rsplit(":", 1)[1])
        await callback.answer()
        if not db.template(key):
            return
        await state.clear()
        await state.set_data({"template_edit_id": key})
        await state.set_state(TemplateFlow.rename)
        await panel(callback, "Пришли новое название заготовки.", reply_markup=cancel_keyboard())

    @admin.message(TemplateFlow.rename)
    async def rename_template(message: Message, state: FSMContext):
        name = (message.text or "").strip()
        if not 1 <= len(name) <= 60:
            await panel(message, "Название должно содержать от 1 до 60 символов.")
            return
        data = await state.get_data()
        try:
            db.execute("UPDATE delivery_templates SET name=? WHERE id=?",
                       (name, data["template_edit_id"]))
        except sqlite3.IntegrityError:
            await panel(message, "Такая заготовка уже существует.")
            return
        await state.clear()
        await panel(message, "Название заготовки изменено.", reply_markup=admin_keyboard())

    @admin.callback_query(F.data.startswith("a:templatedelask:"))
    async def template_delete_ask(callback: CallbackQuery):
        await callback.answer()
        key = int(callback.data.rsplit(":", 1)[1])
        await panel(callback, "Удалить заготовку? Уже назначенный файлам текст останется без изменений.",
                                      reply_markup=keyboard([("Удалить", f"a:templatedelete:{key}")],
                                                            [("Отмена", f"a:template:{key}")]))

    @admin.callback_query(F.data.startswith("a:templatedelete:"))
    async def template_delete(callback: CallbackQuery):
        await callback.answer()
        key = int(callback.data.rsplit(":", 1)[1])
        db.execute("DELETE FROM delivery_templates WHERE id=?", (key,))
        await panel(callback, "Заготовка удалена.", reply_markup=admin_keyboard())

    @admin.callback_query(F.data.startswith("a:askfiledelete:"))
    async def ask_file_delete(callback: CallbackQuery):
        await callback.answer()
        key = callback.data.split(":",2)[2]
        await panel(callback, f"Удалить файл ID {key}? Старая ссылка перестанет работать. "
                                      "Связанный пост и документ в канале/чате-хранилище будут удалены, если Telegram разрешает.",
                                      reply_markup=keyboard([("Удалить файл", f"a:filedelete:{key}"), ("Отмена", f"a:file:{key}")]))

    @admin.callback_query(F.data.startswith("a:filedelete:"))
    async def file_delete(callback: CallbackQuery):
        await callback.answer("Удаляю…")
        try:
            async with service.publish_lock:
                text = await service.delete_file(callback.data.split(":",2)[2])
            await panel(callback, text, reply_markup=admin_keyboard())
        except ValueError as exc:
            await panel(callback, str(exc))

    @admin.callback_query((F.data == "a:sponsors") | F.data.startswith("a:sponsors:"))
    async def sponsors(callback: CallbackQuery):
        await callback.answer()
        offset = (max(0, int(callback.data.rsplit(":", 1)[1]))
                  if callback.data.startswith("a:sponsors:") else 0)
        items = db.sponsors()
        rows = [[(s["title"][:45], f"a:sponsor:{s['chat_id']}")] for s in items[offset:offset + 8]]
        nav = []
        if offset:
            nav.append(("Назад", f"a:sponsors:{max(0, offset - 8)}"))
        if offset + 8 < len(items):
            nav.append(("Далее", f"a:sponsors:{offset + 8}"))
        if nav:
            rows.append(nav)
        rows.append([("Добавить спонсора", "a:addsponsor"), ("Меню", "a:menu")])
        await panel(callback, "Каналы обязательной подписки. Выбери канал для просмотра или удаления.\n"
                    f"Всего спонсоров: {len(items)}.", reply_markup=keyboard(*rows))

    @admin.callback_query(F.data.startswith("a:sponsor:"))
    async def sponsor_card(callback: CallbackQuery):
        await callback.answer()
        cid = int(callback.data.rsplit(":", 1)[1])
        sponsor = db.one("SELECT * FROM sponsors WHERE chat_id=?", (cid,))
        if not sponsor:
            await panel(callback, "Канал уже удалён.", reply_markup=keyboard([("К спонсорам", "a:sponsors")]))
            return
        await panel(callback, f"Спонсор: {sponsor['title']}\nID: {cid}\n{sponsor['url']}",
                    reply_markup=keyboard([("Убрать подписку", f"a:delsponsor:{cid}")],
                                          [("К спонсорам", "a:sponsors")]))

    @admin.callback_query(F.data == "a:addsponsor")
    async def add_sponsor(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_state(SponsorFlow.channel)
        await callback.answer()
        await panel(callback, "Добавь бота администратором канала-спонсора.\n"
                                      "Затем пришли @username публичного канала, числовой ID приватного "
                                      "или перешли сообщение из него.", reply_markup=cancel_keyboard())

    @admin.message(SponsorFlow.channel)
    async def sponsor_channel(message: Message, state: FSMContext):
        try:
            if isinstance(message.forward_origin, MessageOriginChannel):
                channel = message.forward_origin.chat.id
            else:
                channel = channel_ref(message.text or "")
            chat, own = await service.channels.inspect(channel)
            if chat["id"] == cfg.storage_channel_id:
                raise ValueError("Канал файлов нельзя сделать спонсором: он должен оставаться приватным хранилищем.")
            if len(db.sponsors()) >= 30 and not db.one("SELECT chat_id FROM sponsors WHERE chat_id=?", (chat["id"],)):
                raise ValueError("Достигнут лимит: 30 спонсоров.")
            if chat.get("username"):
                url = f"https://t.me/{chat['username']}"
                save_sponsor(chat["id"], chat.get("title") or str(chat["id"]), url)
                await state.clear()
                await panel(message, "Спонсор добавлен.", reply_markup=admin_keyboard())
            else:
                await state.update_data(sponsor_id=chat["id"], sponsor_title=chat.get("title") or str(chat["id"]))
                await state.set_state(SponsorFlow.link)
                await panel(message, "Пришли действующую ссылку приглашения в этот приватный канал. "
                                     "Лучше без заявки на вступление: заявка ещё не считается подпиской.",
                                     reply_markup=cancel_keyboard())
        except (ValueError, TelegramAPIError) as exc:
            await panel(message, friendly_error(exc))

    def save_sponsor(chat_id, title, url):
        db.execute("INSERT INTO sponsors(chat_id,title,url) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,url=excluded.url",
                   (chat_id, title, url))

    @admin.message(SponsorFlow.link)
    async def sponsor_link(message: Message, state: FSMContext):
        try:
            url = valid_url(message.text or "")
            from urllib.parse import urlsplit
            parsed = urlsplit(url)
            if parsed.hostname not in ("t.me", "telegram.me") or not parsed.path.startswith(("/+", "/joinchat/")):
                raise ValueError("Нужна приватная ссылка вида https://t.me/+… или https://t.me/joinchat/…")
        except ValueError as exc:
            await panel(message, str(exc))
            return
        data = await state.get_data()
        save_sponsor(data["sponsor_id"], data["sponsor_title"], url)
        await state.clear()
        await panel(message, "Приватный спонсор добавлен. Проверь приглашение вручную: бот не может "
                             "проверить чужую ссылку приглашения через Bot API.", reply_markup=admin_keyboard())

    @admin.callback_query(F.data.startswith("a:delsponsor:"))
    async def del_sponsor(callback: CallbackQuery):
        channel_id = int(callback.data.rsplit(":", 1)[1])
        row = db.one("SELECT title FROM sponsors WHERE chat_id=?", (channel_id,))
        await callback.answer()
        if not row:
            await panel(callback, "Спонсор уже удалён.")
            return
        await panel(callback, f"Убрать обязательную подписку на {row['title']} для всех файлов?",
                                      reply_markup=keyboard([("Убрать", f"a:confirmsponsor:{channel_id}"), ("Отмена", f"a:sponsor:{channel_id}")]))

    @admin.callback_query(F.data.startswith("a:confirmsponsor:"))
    async def confirm_del_sponsor(callback: CallbackQuery):
        db.execute("DELETE FROM sponsors WHERE chat_id=?", (int(callback.data.rsplit(":", 1)[1]),))
        await callback.answer("Спонсор убран")
        await panel(callback, "Обязательная подписка отключена для этого канала.", reply_markup=admin_keyboard())

    @admin.callback_query(F.data == "a:diagnostics")
    async def diagnostics(callback: CallbackQuery):
        await callback.answer()
        await diagnostic_to(callback)

    async def diagnostic_to(message):
        import aiogram
        import platform
        lines = [f"Бот: @{service.username}", f"ID бота: {bot.id}",
                 f"Python: {platform.python_version()}", f"aiogram: {aiogram.__version__}",
                 f"Файлов: {db.one('SELECT COUNT(*) n FROM files')['n']}",
                 f"Публикаций: {db.one('SELECT COUNT(*) n FROM posts')['n']}",
                 f"Копий постов: {db.one('SELECT COUNT(*) n FROM post_publications')['n']}"]
        for label, ref, purpose in [("Основной канал", cfg.main_channel_id, "main"),
                                    ("Хранилище файлов", cfg.storage_channel_id, "storage")]:
            ok, report = await service.channels.report(ref, purpose)
            lines.extend(["", f"{label} — {'OK' if ok else 'требует внимания'}", report])
        for sponsor in db.sponsors():
            ok, report = await service.channels.report(sponsor["chat_id"])
            lines.extend(["", f"Спонсор {sponsor['title']} — {'OK' if ok else 'требует внимания'}", report])
        full = "\n".join(lines)
        await panel(message, full[:3900] + ("\n… Остальные строки не поместились." if len(full) > 3900 else ""),
                    reply_markup=keyboard([("Меню", "a:menu")]))

    @admin.message(Command("diag"))
    async def diagnostic_command(message: Message):
        await diagnostic_to(message)

    @admin.callback_query(F.data == "a:channels")
    async def channels_menu(callback: CallbackQuery):
        await callback.answer()
        await panel(callback, f"Основной канал: {cfg.main_channel_id or 'не привязан'}\n"
                                      f"Канал / чат файлов: {cfg.storage_channel_id or 'не привязан'}\n\n"
                                      "Хранилищем может быть канал, группа или супергруппа. "
                                      "Для группы можно отправить команду /storage прямо в ней от своего аккаунта.",
                                      reply_markup=keyboard([("Основной канал", "a:bind:main")],
                                                              [("Канал / чат файлов", "a:bind:storage")],
                                                              [("Проверить доступ", "a:diagnostics")],
                                                              [("Меню", "a:menu")]))

    @admin.callback_query(F.data.in_({"a:bind:main", "a:bind:storage"}))
    async def bind_channel(callback: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_data({"purpose": callback.data.rsplit(":", 1)[1]})
        await state.set_state(ChannelFlow.target)
        await callback.answer()
        await panel(callback, "Пришли отрицательный ID, @username или ссылку на пост. "
                                      "Для канала также подходит пересылка его поста. "
                                      "Чтобы привязать группу-хранилище без поиска ID, отправь /storage в самой группе от своего аккаунта.",
                                      reply_markup=cancel_keyboard())

    @admin.message(ChannelFlow.target)
    async def bind_target(message: Message, state: FSMContext):
        data = await state.get_data()
        purpose = data["purpose"]
        try:
            origin = message.forward_origin
            if isinstance(origin, MessageOriginChannel):
                ref = origin.chat.id
            elif isinstance(origin, MessageOriginChat):
                ref = origin.sender_chat.id
            elif origin and purpose == "storage":
                raise ValueError("Обычная пересылка из группы не содержит ID группы. Отправь /storage в самой группе или пришли её числовой ID.")
            else:
                ref = channel_ref(message.text or "")
            chat, own = await service.channels.inspect(ref, purpose)
            if purpose == "storage" and db.one("SELECT chat_id FROM sponsors WHERE chat_id=?", (chat["id"],)):
                raise ValueError("Этот канал уже добавлен спонсором. Сначала убери его из спонсоров.")
            cfg.save_channels(**{purpose: chat["id"]})
        except (ValueError, TelegramAPIError) as exc:
            await panel(message, friendly_error(exc))
            return
        except OSError:
            await panel(message, "Не удалось сохранить .env. Разреши Pydroid доступ к папке проекта.")
            return
        await state.clear()
        await panel(message, f"Канал / чат привязан: {chat.get('title', '')}\nID: {chat['id']}\n"
                             "Настройка сохранена. Перезапуск не нужен.", reply_markup=admin_keyboard())

    # ====================== РЕКЛАМА ======================
    # Объявления независимы от файлов, постов и проверки подписок.

    def ad_menu_keyboard(ad):
        key = ad['id']
        rows = [[('Посмотреть рекламу', f'a:adpreview:{key}')]]
        if ad['status'] != 'deleted':
            rows.extend([
                [('Отправить всем', f'a:adsendask:{key}')],
                [('Изменить фото / видео / стикер', f'a:adeditmedia:{key}')],
                [('Изменить текст', f'a:adedittext:{key}'), ('Изменить кнопки', f'a:adeditbuttons:{key}')],
                [('Переименовать', f'a:adrename:{key}')],
                [('Удалить рекламу у всех', f'a:addeleteask:{key}')],
            ])
        rows.append([('Статус рассылок', f'a:adstatus:{key}')])
        if ad['status'] == 'deleted':
            rows.append([('Повторить удаление у всех', f'a:addelete:{key}')])
        else:
            rows.append([('Обновить у всех ещё раз', f'a:adretry:{key}')])
        rows.append([('К рекламе', 'a:ads'), ('Админка', 'a:menu')])
        return keyboard(*rows)

    async def show_ad_card(event, ad_id):
        ad = db.ad(ad_id)
        if not ad:
            await panel(event, 'Объявление не найдено.', reply_markup=keyboard([('Реклама', 'a:ads')]))
            return
        stats = db.ad_stats(ad_id)
        await panel(event, f"{ad['title']} · №{ad_id}\n"
                    f"Тип: {ad['content']['kind']}\n"
                    f"Статус: {ad['status']}\n"
                    f"Отправленных сообщений, ещё не удалённых: {stats['active']}\n"
                    f"Удалённых: {stats['removed']}\n"
                    f"Запусков операций: {stats['jobs']}",
                    reply_markup=ad_menu_keyboard(ad))

    @admin.callback_query((F.data == 'a:ads') | F.data.startswith('a:ads:'))
    async def ads_menu(callback: CallbackQuery):
        await callback.answer()
        try:
            offset = max(0, int(callback.data.rsplit(':',1)[1])) if callback.data != 'a:ads' else 0
        except ValueError:
            offset = 0
        rows = db.all("SELECT id,title,status FROM ads ORDER BY id DESC LIMIT 9 OFFSET ?", (offset,))
        keys = [[(f"{x['title'][:34]} · {x['status']}", f"a:ad:{x['id']}")] for x in rows[:8]]
        nav = []
        if offset:
            nav.append(('Назад', f'a:ads:{max(0, offset-8)}'))
        if len(rows)>8:
            nav.append(('Далее', f'a:ads:{offset+8}'))
        if nav:
            keys.append(nav)
        keys.append([('Создать рекламу', 'a:adnew')])
        keys.append([('Админка', 'a:menu')])
        audience = db.one('SELECT COUNT(*) n FROM bot_users WHERE blocked=0')['n']
        await panel(callback, f'Реклама @SimpleDLC\nИзвестных доступных пользователей: {audience}\n'
                    'Бот сможет отправить объявления только тем, кто уже открывал его.\n'
                    'Каждая рассылка сохраняет отдельные ID сообщений для последующего редактирования и удаления.',
                    reply_markup=keyboard(*keys))

    @admin.callback_query(F.data.startswith('a:ad:'))
    async def ad_card(callback: CallbackQuery):
        await callback.answer()
        await show_ad_card(callback, int(callback.data.rsplit(':',1)[1]))

    @admin.callback_query(F.data == 'a:adnew')
    async def ad_new(callback: CallbackQuery, state: FSMContext):
        await cleanup_wizard_preview(state, callback.message.chat.id)
        await state.clear()
        await state.set_data({'mode':'new', 'buttons': [], 'content': None})
        await state.set_state(AdFlow.media)
        await callback.answer()
        await panel(callback, 'Новая реклама. Отправь фото, видео, GIF или Telegram-стикер. '
                    'Текст следующим сообщением. Можно создать рекламу только с текстом.',
                    reply_markup=keyboard([('Только текст', 'a:adtextonly')],
                                          [('Отмена', 'a:cancel')]))

    @admin.callback_query(AdFlow.media, F.data == 'a:adtextonly')
    async def ad_text_only(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        await state.update_data(content={'kind':'text','file_id':None,'text':'','entities':[],
                                         'above':False,'spoiler':False})
        await state.set_state(AdFlow.text)
        await callback.answer()
        await panel(callback, 'Отправь текст рекламы с нужным форматированием и премиум-эмодзи.',
                    reply_markup=cancel_keyboard())

    @admin.message(AdFlow.media)
    async def ad_receive_media(message: Message, state: FSMContext):
        data = await state.get_data()
        try:
            content = snapshot(message)
        except ValueError as exc:
            await panel(message, str(exc))
            return
        if content['kind'] == 'text':
            await state.update_data(content=content)
            await ad_advance(message, state, 'content')
            return
        content['text'], content['entities'] = '', []
        if data.get('mode')=='edit':
            old = data['content']
            content['text'], content['entities'] = old.get('text',''), old.get('entities',[])
            await state.update_data(content=content)
            await ad_advance(message, state, 'content')
        else:
            await state.update_data(content=content)
            await state.set_state(AdFlow.text)
            await panel(message, 'Медиа получено. Теперь отправь текст объявления отдельно. '
                        'Premium-эмодзи и форматирование сохраняются.',
                        reply_markup=keyboard([('Без текста', 'a:adskiptext')],
                                              [('Отмена', 'a:cancel')]))

    @admin.message(AdFlow.text)
    async def ad_receive_text(message: Message, state: FSMContext):
        if message.text is None:
            await panel(message, 'Отправь обычное текстовое сообщение с форматированием Telegram.')
            return
        data = await state.get_data()
        content = data['content']
        content['text'] = message.text
        content['entities'] = [e.model_dump(mode='json',exclude_none=True) for e in (message.entities or [])]
        try:
            validate_ad(content, data.get('buttons', []))
        except ValueError as exc:
            await panel(message, str(exc))
            return
        await state.update_data(content=content)
        await ad_advance(message, state, 'content')

    @admin.callback_query(AdFlow.text, F.data == 'a:adskiptext')
    async def ad_skip_text(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        content = data.get('content') or {}
        if content.get('kind')=='text':
            await callback.answer('Нельзя пропустить текст, если нет медиа.', show_alert=True)
            return
        content['text'],content['entities']='',[]
        await state.update_data(content=content)
        await callback.answer()
        await ad_advance(callback, state, 'content')

    async def ad_advance(event, state, changed):
        data = await state.get_data()
        if data.get('mode')=='edit':
            await state.set_state(AdFlow.confirm)
            await ad_confirm(event, state)
        else:
            await state.set_state(AdFlow.buttons)
            await panel(event, 'Добавь инлайн-кнопки. Каждая строка: Название | https://example.com\n'
                        'Можно указать несколько кнопок, по одной в строке.',
                        reply_markup=keyboard([('Без кнопок', 'a:adnobuttons')],
                                              [('Отмена', 'a:cancel')]))

    def parse_ad_buttons(text):
        items = []
        for line in (text or '').splitlines():
            if not line.strip():
                continue
            if '|' not in line:
                raise ValueError('Формат кнопки: Название | https://example.com (каждая с новой строки).')
            label, url = (part.strip() for part in line.split('|', 1))
            if not label or len(label)>64:
                raise ValueError('Название кнопки должно содержать от 1 до 64 символов.')
            items.append({'text':label,'url':valid_url(url)})
        if not items or len(items)>12:
            raise ValueError('Добавь от 1 до 12 кнопок или выбери «Без кнопок».')
        return items

    @admin.message(AdFlow.buttons)
    async def ad_receive_buttons(message: Message, state: FSMContext):
        try:
            buttons = parse_ad_buttons(message.text)
        except ValueError as exc:
            await panel(message, str(exc))
            return
        await state.update_data(buttons=buttons)
        await state.set_state(AdFlow.confirm)
        await ad_confirm(message, state)

    @admin.callback_query(AdFlow.buttons, F.data == 'a:adnobuttons')
    async def ad_skip_buttons(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(buttons=[])
        await state.set_state(AdFlow.confirm)
        await ad_confirm(callback, state)

    async def ad_confirm(event, state):
        data = await state.get_data()
        content, buttons = data.get('content'), data.get('buttons',[])
        if not content:
            await panel(event, 'Нет подготовленного объявления. Начни заново.',
                        reply_markup=keyboard([('Реклама','a:ads')]))
            return
        preview = (content.get('text') or '(без текста)')[:450]
        warning = ''
        if data.get('mode')=='edit':
            previous = db.ad(data['ad_id'])
            before = previous['content']
            if (before['kind'] != content['kind'] or
                    (before['kind']=='sticker' and before.get('file_id') != content.get('file_id'))):
                warning = ('\nВажно: этот тип сообщения нельзя отредактировать на месте. '
                           'Бот отправит замену и удалит старое сообщение. '
                           'Если прошло более 48 часов, Telegram может отказать в удалении. '
                           'Ошибки будут видны в статусе.')
        label = 'Обновить у всех получателей' if data.get('mode')=='edit' else 'Сохранить рекламу'
        await panel(event, 'Предпросмотр рекламы (текст):\n'+preview+
                    f'\nТип медиа: {content["kind"]}\nКнопок: {len(buttons)}'+warning,
                    reply_markup=keyboard([('Посмотреть целиком', 'a:adflowpreview')],
                                          [(label, 'a:adsave')],
                                          [('Изменить медиа', 'a:adflowmedia'), ('Изменить текст', 'a:adflowtext')],
                                          [('Изменить кнопки', 'a:adflowbuttons')],
                                          [('Отмена', 'a:cancel')]))

    @admin.callback_query(AdFlow.confirm, F.data == 'a:adflowpreview')
    async def ad_wizard_preview(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        try:
            await send_ad(bot, callback.message.chat.id, data['content'],data.get('buttons',[]))
        except TelegramAPIError as exc:
            await panel(callback, 'Telegram отказал в отправке предпросмотра. '
                        'Проверь premium-права владельца бота или формат медиа. '+friendly_error(exc),
                        reply_markup=keyboard([('Назад', 'a:adflowconfirm')]))

    @admin.callback_query(AdFlow.confirm, F.data == 'a:adflowconfirm')
    async def ad_back_confirm(callback: CallbackQuery, state: FSMContext):
        await callback.answer()
        await ad_confirm(callback, state)

    @admin.callback_query(AdFlow.confirm, F.data.in_({'a:adflowmedia','a:adflowtext','a:adflowbuttons'}))
    async def ad_wizard_change(callback: CallbackQuery, state: FSMContext):
        target = callback.data.rsplit('flow',1)[1]
        await callback.answer()
        if target=='media':
            await state.set_state(AdFlow.media)
            await panel(callback, 'Пришли новое фото/видео/GIF/стикер или текст для рекламы.',
                        reply_markup=keyboard([('Только текст','a:adtextonly')], [('Отмена','a:cancel')]))
        elif target=='text':
            await state.set_state(AdFlow.text)
            await panel(callback, 'Пришли новый текст с форматированием.',
                        reply_markup=keyboard([('Без текста','a:adskiptext')], [('Отмена','a:cancel')]))
        else:
            await state.set_state(AdFlow.buttons)
            await panel(callback, 'Пришли список кнопок: Название | https://ссылка (одна на строку).',
                        reply_markup=keyboard([('Без кнопок','a:adnobuttons')], [('Отмена','a:cancel')]))

    @admin.callback_query(AdFlow.confirm, F.data == 'a:adsave')
    async def ad_save(callback: CallbackQuery, state: FSMContext):
        data = await state.get_data()
        try:
            content = validate_ad(data['content'],data.get('buttons',[]))
            ad_id = data.get('ad_id') if data.get('mode')=='edit' else None
            if ad_id and db.one("SELECT id FROM ad_jobs WHERE ad_id=? AND status IN ('pending','running')",(ad_id,)):
                raise ValueError('Сначала дождись завершения текущей операции.')
            ad_id = db.save_ad(content, data.get('buttons', []), ad_id=ad_id)
            job_id = None
            if data.get('mode')=='edit' and db.ad_stats(ad_id)['active']:
                job_id = db.queue_ad_job(ad_id,'edit')
                ads.launch(job_id)
        except (ValueError,TelegramAPIError) as exc:
            await callback.answer(friendly_error(exc)[:180], show_alert=True)
            return
        await callback.answer('Сохранено')
        await state.clear()
        await panel(callback, f'Реклама №{ad_id} сохранена.'+
                    (f' Начато обновление ранее отправленных сообщений, операция №{job_id}.' if job_id else ''),
                    reply_markup=ad_menu_keyboard(db.ad(ad_id)))

    @admin.callback_query(F.data.startswith('a:adeditmedia:') | F.data.startswith('a:adedittext:') | F.data.startswith('a:adeditbuttons:'))
    async def ad_edit(callback: CallbackQuery, state: FSMContext):
        ad_id = int(callback.data.rsplit(':',1)[1])
        ad = db.ad(ad_id)
        if not ad or ad['status']=='deleted':
            await callback.answer('Реклама недоступна.', show_alert=True)
            return
        if db.one("SELECT id FROM ad_jobs WHERE ad_id=? AND status IN ('pending','running')",(ad_id,)):
            await callback.answer('Сначала дождись завершения рассылки.', show_alert=True)
            return
        target = 'media' if callback.data.startswith('a:adeditmedia:') else 'text' if callback.data.startswith('a:adedittext:') else 'buttons'
        await state.clear()
        await state.set_data({'mode':'edit','ad_id':ad_id,'content':ad['content'],'buttons':ad['buttons']})
        await state.set_state({'media':AdFlow.media,'text':AdFlow.text,'buttons':AdFlow.buttons}[target])
        await callback.answer()
        if target=='media':
            await panel(callback, 'Пришли новое фото/видео/GIF/стикер. Смена типа между стикером и фото/видео '
                        'потребует удалить старое сообщение (Telegram может отказать после 48 часов). ',
                        reply_markup=keyboard([('Только текст','a:adtextonly')], [('Отмена','a:cancel')]))
        elif target=='text':
            await panel(callback, 'Пришли новый текст. Для фото и видео максимум 1024 символа.',
                        reply_markup=keyboard([('Без текста','a:adskiptext')], [('Отмена','a:cancel')]))
        else:
            await panel(callback, 'Пришли кнопки: Название | https://ссылка (каждая с новой строки).',
                        reply_markup=keyboard([('Без кнопок','a:adnobuttons')], [('Отмена','a:cancel')]))

    @admin.callback_query(F.data.startswith('a:adrename:'))
    async def ad_rename_prompt(callback: CallbackQuery, state: FSMContext):
        ad_id = int(callback.data.rsplit(':',1)[1])
        if not db.ad(ad_id):
            await callback.answer('Реклама не найдена.',show_alert=True)
            return
        await state.clear()
        await state.set_data({'ad_id':ad_id})
        await state.set_state(AdFlow.title)
        await callback.answer()
        await panel(callback, 'Напиши название рекламы для списка в админке.',reply_markup=cancel_keyboard())

    @admin.message(AdFlow.title)
    async def ad_set_title(message: Message, state: FSMContext):
        title = (message.text or '').strip()
        if not (1 <= len(title) <= 60):
            await panel(message, 'Название должно содержать от 1 до 60 символов.')
            return
        data = await state.get_data()
        db.execute('UPDATE ads SET title=? WHERE id=?', (title,data['ad_id']))
        await state.clear()
        await show_ad_card(message,data['ad_id'])

    @admin.callback_query(F.data.startswith('a:adpreview:'))
    async def ad_preview(callback: CallbackQuery):
        await callback.answer()
        ad = db.ad(int(callback.data.rsplit(':',1)[1]))
        if not ad:
            return
        try:
            await send_ad(bot, callback.message.chat.id, ad['content'],ad['buttons'])
        except TelegramAPIError as exc:
            await panel(callback, 'Не удалось показать рекламу: '+friendly_error(exc),
                        reply_markup=ad_menu_keyboard(ad))

    @admin.callback_query(F.data.startswith('a:adsendask:'))
    async def ad_send_confirm(callback: CallbackQuery):
        ad_id = int(callback.data.rsplit(':',1)[1])
        await callback.answer()
        total = db.one('SELECT COUNT(*) n FROM bot_users WHERE blocked=0')['n']
        await panel(callback, f'Отправить рекламу №{ad_id} всем известным пользователям бота?\n'
                    f'Получателей в базе: {total}. Если человек заблокировал бота, отправка завершится ошибкой.\n'
                    'Telegram не позволяет боту первым писать людям, не открывавшим его.',
                    reply_markup=keyboard([('Подтвердить рассылку','a:adsend:'+str(ad_id))],
                                          [('Отмена','a:ad:'+str(ad_id))]))

    @admin.callback_query(F.data.startswith('a:adsend:'))
    async def ad_send_start(callback: CallbackQuery):
        ad_id = int(callback.data.rsplit(':',1)[1])
        try:
            job_id = db.queue_ad_job(ad_id,'send')
            ads.launch(job_id)
        except ValueError as exc:
            await callback.answer(str(exc)[:190],show_alert=True)
            return
        await callback.answer('Рассылка запущена')
        await panel(callback, f'Рассылка рекламы №{ad_id} запущена. Задание №{job_id}.\n'
                    'Прогресс и ошибки доступны в статусе.',
                    reply_markup=keyboard([('Статус','a:adstatus:'+str(ad_id))],
                                          [('К рекламе','a:ad:'+str(ad_id))]))

    @admin.callback_query(F.data.startswith('a:adstatus:'))
    async def ad_status(callback: CallbackQuery):
        ad_id = int(callback.data.rsplit(':',1)[1])
        await callback.answer()
        latest = db.ad_latest_job(ad_id)
        if not latest:
            await panel(callback,'Для этой рекламы операций ещё не было.',
                        reply_markup=keyboard([('К рекламе','a:ad:'+str(ad_id))]))
            return
        job = db.ad_job(latest['id'])
        errors = db.all("""SELECT chat_id,error FROM ad_job_targets
            WHERE job_id=? AND status='failed' ORDER BY id LIMIT 5""", (job['id'],))
        tail = '\n'.join(f"{r['chat_id']}: {r['error'][:120]}" for r in errors)
        await panel(callback, f"Операция №{job['id']} · {job['kind']} · {job['status']}\n"
                    f"Всего: {job['total']}\nУспешно: {job['done']}\nОшибок: {job['failed']}"
                    +(f'\nПримеры ошибок:\n{tail}' if tail else ''),
                    reply_markup=keyboard([('Обновить статус','a:adstatus:'+str(ad_id))],
                                          [('К рекламе','a:ad:'+str(ad_id))]))

    @admin.callback_query(F.data.startswith('a:adretry:'))
    async def ad_retry_edit(callback: CallbackQuery):
        ad_id = int(callback.data.rsplit(':',1)[1])
        try:
            job_id = db.queue_ad_job(ad_id,'edit')
            ads.launch(job_id)
        except ValueError as exc:
            await callback.answer(str(exc)[:180],show_alert=True)
            return
        await callback.answer('Обновление запущено')
        await panel(callback, f'Повторное обновление №{job_id} запущено.',
                    reply_markup=keyboard([('Статус','a:adstatus:'+str(ad_id))]))

    @admin.callback_query(F.data.startswith('a:addeleteask:'))
    async def ad_delete_ask(callback: CallbackQuery):
        ad_id = int(callback.data.rsplit(':',1)[1])
        await callback.answer()
        await panel(callback, f'Удалить рекламу №{ad_id} у всех получателей? '
                    'Telegram обычно разрешает удалять сообщения не старше 48 часов. '
                    'Для более старых сообщений сервер может отказать; они останутся в отчёте.',
                    reply_markup=keyboard([('Да, удалить у всех','a:addelete:'+str(ad_id))],
                                          [('Нет','a:ad:'+str(ad_id))]))

    @admin.callback_query(F.data.startswith('a:addelete:'))
    async def ad_delete_start(callback: CallbackQuery):
        ad_id = int(callback.data.rsplit(':',1)[1])
        try:
            job_id = db.queue_ad_job(ad_id,'delete')
            ads.launch(job_id)
        except ValueError as exc:
            await callback.answer(str(exc)[:180],show_alert=True)
            return
        await callback.answer('Удаление запущено')
        await panel(callback, f'Удаление рекламы №{ad_id} запущено. Операция №{job_id}.',
                    reply_markup=keyboard([('Статус','a:adstatus:'+str(ad_id))],
                                          [('К рекламе','a:ad:'+str(ad_id))]))

    @admin.callback_query(F.data.startswith("a:"))
    async def stale_button(callback: CallbackQuery):
        await callback.answer("Эта кнопка устарела. Открой /admin или /resume.", show_alert=True)

    @admin.message(StateFilter("*"), F.text.startswith("/"))
    async def unknown_command(message: Message):
        await panel(message, "Команды: /admin, /diag, /resume, /cancel, /id.")

    @admin.message(StateFilter(None), F.document)
    async def direct_upload(message: Message, state: FSMContext):
        await uploaded_file(message, state)

    root.include_routers(storage, public, admin)
    return (root,)


def friendly_error(exc):
    log.warning("Действие отклонено: %s", explain_error(exc))
    return explain_error(exc)
