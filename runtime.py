"""Long polling, безопасные логи, мягкие проверки каналов и остановка дублей."""
import asyncio
import hashlib
import logging
import re
import socket
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramConflictError, TelegramNetworkError
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat, ErrorEvent

from api import Channels, explain_error, private_recipient_unavailable, expired_callback
from database import Database, SQLiteStorage
from handlers import build_routers
from service import Service
from settings import ROOT

log = logging.getLogger(__name__)


class RedactingFormatter(logging.Formatter):
    def __init__(self, token):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.token = token

    def format(self, record):
        text = super().format(record).replace(self.token, "<BOT_TOKEN>")
        return re.sub(r"\b\d{5,15}:[A-Za-z0-9_-]{20,}\b", "<BOT_TOKEN>", text)


def configure_logging(token):
    folder = ROOT / "data"
    folder.mkdir(exist_ok=True)
    handlers = [logging.StreamHandler(), RotatingFileHandler(
        folder / "bot.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    for handler in handlers:
        handler.setFormatter(RedactingFormatter(token))
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def instance_guard(token):
    """Локальный порт освобождается ОС даже после принудительного закрытия Pydroid."""
    port = 20000 + int.from_bytes(hashlib.sha256(token.encode()).digest()[:2], "big") % 30000
    handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        handle.bind(("127.0.0.1", port))
        handle.listen(1)
    except OSError:
        handle.close()
        raise ValueError("Уже запущен экземпляр этого бота на телефоне (или занят его локальный порт). "
                         "Останови прежний запуск в Pydroid.") from None
    return handle


async def prepare_channels(bot, cfg):
    """Проблемы каналов сообщаем, сохраняя возможность исправить их через /admin."""
    channels = Channels(bot)
    reports = []
    for purpose, label, ref in (("storage", "Хранилище файлов", cfg.storage_channel_id),
                                ("main", "Основной канал", cfg.main_channel_id)):
        try:
            chat, own = await channels.inspect(ref, purpose)
            if chat["id"] != ref:
                key = f"{purpose.upper()}_CHANNEL_ID"
                if key in cfg.external_channels:
                    # Значение окружения не перезаписываем; в работе используем числовой ID.
                    setattr(cfg, f"{purpose}_channel_id", chat["id"])
                else:
                    cfg.save_channels(**{purpose: chat["id"]})
            report = f"{label}: OK, {chat.get('title', '')}, ID {chat['id']}"
        except (TelegramAPIError, ValueError) as exc:
            report = f"{label}: {explain_error(exc)}"
        except OSError:
            report = f"{label}: не удалось сохранить ID в .env. Проверь доступ Pydroid к папке."
        log.info(report)
        reports.append(report)
    return reports


async def identify(bot):
    for attempt in range(3):
        try:
            return await bot.get_me()
        except TelegramNetworkError:
            if attempt == 2:
                raise
            log.warning("Telegram пока недоступен. Повторное подключение через 2 секунды…")
            await asyncio.sleep(2)


async def run(cfg):
    guard = instance_guard(cfg.bot_token)
    db = None
    bot = None
    dp = None
    service = None
    try:
        (ROOT / "data").mkdir(exist_ok=True)
        db = Database(ROOT / "data" / "bot.sqlite3")
        bot = Bot(token=cfg.bot_token)
        dp = Dispatcher(storage=SQLiteStorage(db), events_isolation=SimpleEventIsolation())
        me = await identify(bot)
        log.info("Подключён бот @%s, ID %s", me.username, me.id)
        for warning in cfg.warnings:
            log.warning(warning)
        await prepare_channels(bot, cfg)
        service = Service(bot, db, cfg, me.username)
        dp.include_routers(*build_routers(service))
        # Возобновляем незаконченные рассылки после перезапуска Pydroid.
        service.ads.resume()

        @dp.errors()
        async def on_error(event: ErrorEvent):
            exc = event.exception
            if private_recipient_unavailable(exc) or expired_callback(exc):
                log.info("Ответ недоступному личному чату или устаревшей кнопке пропущен.")
                return True
            log.error("Ошибка действия: %s", explain_error(exc), exc_info=(type(exc), exc, exc.__traceback__))
            callback, message = event.update.callback_query, event.update.message
            user = callback.from_user if callback else (message.from_user if message else None)
            target = callback.message if callback else message
            try:
                if callback:
                    try:
                        await callback.answer("Действие не завершено", show_alert=True)
                    except TelegramAPIError:
                        pass
                if target and target.chat.type == "private":
                    text = explain_error(exc) if user and user.id in cfg.admin_ids else "Не удалось выполнить действие. Повтори позже."
                    await bot.send_message(target.chat.id, text)
            except TelegramAPIError:
                pass
            return True

        stopping = False

        @bot.session.middleware()
        async def stop_duplicate_request(make_request, bot, method):
            nonlocal stopping
            try:
                return await make_request(bot, method)
            except TelegramConflictError:
                if method.__api_method__ == "getUpdates" and not stopping:
                    stopping = True
                    log.error("Telegram видит второй запущенный экземпляр этого бота. Этот запуск остановлен; "
                              "останови копию на другом устройстве/сервере и запусти заново.")
                    asyncio.create_task(dp.stop_polling())
                raise

        await bot.delete_webhook(drop_pending_updates=False)
        try:
            await bot.set_my_commands([BotCommand(command="start", description="Открыть бота"),
                                       BotCommand(command="id", description="Мой ID")])
            for admin in cfg.admin_ids:
                try:
                    await bot.set_my_commands([
                        BotCommand(command="admin", description="Управление ботом"),
                        BotCommand(command="diag", description="Проверка каналов и прав"),
                        BotCommand(command="resume", description="Продолжить ввод"),
                        BotCommand(command="cancel", description="Отмена"),
                        BotCommand(command="mysub", description="Моя проверка подписки"),
                        BotCommand(command="id", description="Мой ID")], scope=BotCommandScopeChat(chat_id=admin))
                except TelegramAPIError:
                    log.info("Меню администратора появится после первого обращения. Команда /admin уже доступна.")
        except TelegramAPIError as exc:
            log.warning("Не удалось обновить список команд: %s. Команды доступны текстом.", explain_error(exc))
        log.info("Бот работает. Открой @%s → /admin. Привязка каналов: кнопка «Каналы».", me.username)
        await dp.start_polling(bot, handle_signals=False, close_bot_session=False,
                               allowed_updates=dp.resolve_used_update_types())
    finally:
        if service is not None and hasattr(service, "ads"):
            await service.ads.shutdown()
        if dp:
            await dp.fsm.close()
        if bot:
            await bot.session.close()
        if db:
            db.close()
        guard.close()
