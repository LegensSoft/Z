"""Реклама для пользователей бота: аудитория, редактируемые отправки и восстановимые задания.

Telegram разрешает писать только людям, которые ранее начали диалог с ботом;
удаление сообщений обычно ограничено 48 часами. Не прячем эти ограничения.
"""
import asyncio
import json
import logging

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
                           InputMediaPhoto, InputMediaVideo, InputMediaAnimation, MessageEntity)

from api import explain_error, private_recipient_unavailable

log = logging.getLogger(__name__)


class AudienceRegistry(BaseMiddleware):
    """Учитывает пользователей при первом обращении, даже если не прошли подписку."""
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event, data):
        callback = isinstance(event, CallbackQuery)
        message = event.message if callback else event
        user = getattr(event, 'from_user', None)
        if user and not getattr(user, 'is_bot', False) and message and message.chat.type == 'private':
            self.db.upsert_user(user.id, getattr(user, 'username', None) or '')
        return await handler(event, data)


def ad_keyboard(buttons):
    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=b['text'], url=b['url'])] for b in buttons
    ])


def ad_entities(content):
    return [MessageEntity.model_validate(entity) for entity in content.get('entities', [])]


def validate_ad(content, buttons):
    kind = content.get('kind')
    if kind not in ('text', 'photo', 'video', 'animation', 'sticker'):
        raise ValueError('Реклама должна содержать текст, фото, видео, GIF или стикер.')
    if kind != 'text' and not content.get('file_id'):
        raise ValueError('Не получен Telegram file_id для медиа.')
    text = content.get('text') or ''
    if kind == 'text' and not text:
        raise ValueError('Для рекламы без медиа нужен текст.')
    maximum = 1024 if kind in ('photo', 'video', 'animation') else 4096
    if len(text.encode('utf-16-le')) // 2 > maximum:
        raise ValueError(f'Текст слишком длинный: предел {maximum} символов Telegram.')
    if len(buttons) > 12:
        raise ValueError('Максимум 12 инлайн-кнопок.')
    return content


async def send_ad(bot, chat_id, content, buttons):
    """Возвращает (ID основного сообщения, ID дополнительного текста для стикера)."""
    markup = ad_keyboard(buttons)
    kind, caption = content['kind'], content.get('text') or ''
    ent = ad_entities(content)
    if kind == 'text':
        msg = await bot.send_message(chat_id, caption, entities=ent, parse_mode=None,
                                     reply_markup=markup)
        return msg.message_id, None
    if kind == 'sticker':
        # Подпись у Telegram-стикера невозможна. Текст размещаем отдельным сообщением.
        msg = await bot.send_sticker(chat_id, content['file_id'],
                                     reply_markup=None if caption else markup)
        if not caption:
            return msg.message_id, None
        try:
            extra = await bot.send_message(chat_id, caption, entities=ent, parse_mode=None,
                                           reply_markup=markup)
        except TelegramAPIError:
            try:
                await bot.delete_message(chat_id, msg.message_id)
            except TelegramAPIError:
                pass
            raise
        return msg.message_id, extra.message_id
    kw = {'chat_id': chat_id, 'caption': caption, 'caption_entities': ent,
          'parse_mode': None, 'reply_markup': markup,
          'show_caption_above_media': bool(content.get('above', False)),
          'has_spoiler': bool(content.get('spoiler', False))}
    if kind == 'photo':
        msg = await bot.send_photo(photo=content['file_id'], **kw)
    elif kind == 'video':
        msg = await bot.send_video(video=content['file_id'], **kw)
    else:
        msg = await bot.send_animation(animation=content['file_id'], **kw)
    return msg.message_id, None


def _is_noop(exc):
    return 'message is not modified' in str(exc).lower()


async def _safe_edit(bot, method, **kwargs):
    try:
        return await getattr(bot, method)(**kwargs)
    except TelegramBadRequest as exc:
        if _is_noop(exc):
            return True
        raise


async def _safe_delete(bot, chat_id, message_id):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramBadRequest as exc:
        if 'message to delete not found' not in str(exc).lower():
            raise


async def edit_ad_delivery(bot, delivery, new_content, new_buttons):
    """Редактирует полученное объявление без спама новым сообщением.

    При смене типа текста/медиа, которую Telegram не умеет редактировать,
    операция честно завершается ошибкой (без дублирующей рассылки).
    """
    old = json.loads(delivery['content'])
    old_buttons = json.loads(delivery['buttons'])
    old_kind, new_kind = old['kind'], new_content['kind']
    chat, msg_id, extra_id = delivery['chat_id'], delivery['message_id'], delivery['extra_message_id']
    markup = ad_keyboard(new_buttons)
    text, ent = new_content.get('text') or '', ad_entities(new_content)
    if old_kind != new_kind and (old_kind in ('text','sticker') or new_kind in ('text','sticker')):
        raise ValueError('Telegram не позволяет менять текст/стикер на другой тип у уже отправленного объявления. Создай новую рекламу.')
    if new_kind == 'sticker':
        if old.get('file_id') != new_content.get('file_id'):
            raise ValueError('Telegram не позволяет изменить уже отправленный стикер. Создай новую рекламу.')
        if bool(old.get('text')) != bool(text):
            raise ValueError('Для уже отправленного стикера нельзя добавлять/удалять отдельное текстовое сообщение. Создай новую рекламу.')
        if text:
            if (old.get('text'), old.get('entities'), old_buttons) != (text, new_content.get('entities'), new_buttons):
                await _safe_edit(bot, 'edit_message_text', chat_id=chat, message_id=extra_id,
                                 text=text, entities=ent, parse_mode=None, reply_markup=markup)
        elif old_buttons != new_buttons:
            await _safe_edit(bot, 'edit_message_reply_markup', chat_id=chat,
                             message_id=msg_id, reply_markup=markup)
    elif new_kind == 'text':
        if old != new_content or old_buttons != new_buttons:
            await _safe_edit(bot, 'edit_message_text', chat_id=chat, message_id=msg_id,
                             text=text, entities=ent, parse_mode=None, reply_markup=markup)
    else:
        if old_kind != new_kind or old.get('file_id') != new_content.get('file_id'):
            cls = {'photo': InputMediaPhoto, 'video': InputMediaVideo, 'animation': InputMediaAnimation}[new_kind]
            media = cls(media=new_content['file_id'], caption=text, caption_entities=ent,
                        parse_mode=None, show_caption_above_media=bool(new_content.get('above')),
                        has_spoiler=bool(new_content.get('spoiler')))
            await _safe_edit(bot, 'edit_message_media', chat_id=chat, message_id=msg_id,
                             media=media, reply_markup=markup)
        elif old != new_content:
            await _safe_edit(bot, 'edit_message_caption', chat_id=chat, message_id=msg_id,
                             caption=text, caption_entities=ent, parse_mode=None,
                             show_caption_above_media=bool(new_content.get('above')),
                             reply_markup=markup)
        elif old_buttons != new_buttons:
            await _safe_edit(bot, 'edit_message_reply_markup', chat_id=chat,
                             message_id=msg_id, reply_markup=markup)


async def replace_ad_delivery(bot, delivery, content, buttons):
    """Telegram не умеет менять стикер или тип сообщения на месте.

    Создаём замену, удаляем старые сообщения, и только затем меняем
    ID в реестре. Если старую рекламу удалить нельзя, удаляем новую,
    а ошибку записываем в отчёт (например, ограничение 48 часов).
    """
    chat = delivery['chat_id']
    new_id, new_extra = await send_ad(bot, chat, content, buttons)
    try:
        await _safe_delete(bot, chat, delivery['extra_message_id'])
        await _safe_delete(bot, chat, delivery['message_id'])
    except TelegramAPIError:
        for message_id in (new_extra, new_id):
            if message_id:
                try:
                    await _safe_delete(bot, chat, message_id)
                except TelegramAPIError as exc:
                    log.warning('Не удалось убрать замену у получателя %s: %s', chat, exc)
        raise
    return new_id, new_extra


class AdsBroadcaster:
    """Фоновые задачи внутри запущенного бота, состояние каждой цели в SQLite."""
    def __init__(self, bot, db, admin_ids):
        self.bot, self.db, self.admin_ids = bot, db, admin_ids
        self.tasks = {}

    def launch(self, job_id):
        if job_id in self.tasks and not self.tasks[job_id].done():
            return
        task = asyncio.create_task(self.process(job_id), name=f'ad-job-{job_id}')
        self.tasks[job_id] = task
        task.add_done_callback(lambda _: self.tasks.pop(job_id, None))

    def resume(self):
        for row in self.db.all("SELECT id FROM ad_jobs WHERE status IN ('pending','running') ORDER BY id"):
            self.launch(row['id'])

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def process(self, job_id):
        job = self.db.ad_job(job_id)
        if not job or job['status'] == 'done':
            return
        self.db.execute("UPDATE ad_jobs SET status='running' WHERE id=?", (job_id,))
        content = json.loads(job['content'])
        buttons = json.loads(job['buttons'])
        try:
            while True:
                targets = self.db.all("""SELECT * FROM ad_job_targets
                    WHERE job_id=? AND status='pending' ORDER BY id LIMIT 100""", (job_id,))
                if not targets:
                    break
                for target in targets:
                    # При каждом обращении к Telegram соблюдаем общий темп рассылки.
                    for attempt in range(4):
                        try:
                            await self._process_target(job, target, content, buttons)
                            self.db.execute("UPDATE ad_job_targets SET status='done',error='' WHERE id=?",
                                            (target['id'],))
                            break
                        except TelegramRetryAfter as exc:
                            if attempt == 3:
                                self._fail(target, f'Лимит Telegram: {exc.retry_after} сек.')
                                break
                            await asyncio.sleep(min(max(float(exc.retry_after), 1), 120) + 0.5)
                        except (TelegramAPIError, ValueError) as exc:
                            if job['kind'] == 'send' and private_recipient_unavailable(exc):
                                self.db.execute("UPDATE bot_users SET blocked=1 WHERE user_id=?", (target['chat_id'],))
                            self._fail(target, explain_error(exc)[:400])
                            break
                        except Exception as exc:
                            self._fail(target, f'Неожиданная ошибка: {type(exc).__name__}')
                            log.exception('Ошибка рекламной операции (job %s, target %s)', job_id, target['id'])
                            break
                    await asyncio.sleep(.065)
        except asyncio.CancelledError:
            self.db.execute("UPDATE ad_jobs SET status='pending' WHERE id=?", (job_id,))
            raise
        self.db.execute("UPDATE ad_jobs SET status='done', finished_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
        report = self.db.ad_job(job_id)
        label = {'send':'Рассылка', 'edit':'Обновление', 'delete':'Удаление'}[job['kind']]
        for admin in self.admin_ids:
            try:
                await self.bot.send_message(admin, f'{label} рекламы №{job["ad_id"]} завершено. '
                    f'Успешно: {report["done"]}, ошибок: {report["failed"]}, всего: {report["total"]}.\n'
                    f'Подробности: /admin → Реклама → объявление → Статус.')
            except TelegramAPIError:
                log.warning('Не удалось сообщить администратору о рекламной операции №%s', job_id)

    def _fail(self, target, error):
        self.db.execute("UPDATE ad_job_targets SET status='failed',error=? WHERE id=?",
                        (error, target['id']))
        if target.get('delivery_id'):
            self.db.execute("UPDATE ad_deliveries SET last_error=? WHERE id=?",
                            (error, target['delivery_id']))

    async def _process_target(self, job, target, content, buttons):
        chat_id, kind = target['chat_id'], job['kind']
        if kind == 'send':
            msg_id, extra_id = await send_ad(self.bot, chat_id, content, buttons)
            self.db.execute("""INSERT OR IGNORE INTO ad_deliveries
                (ad_id,chat_id,message_id,extra_message_id,content,buttons)
                VALUES(?,?,?,?,?,?)""", (job['ad_id'], chat_id, msg_id, extra_id,
                        job['content'], job['buttons']))
            self.db.execute("UPDATE ads SET status='sent' WHERE id=? AND status!='deleted'", (job['ad_id'],))
            return
        delivery = self.db.one("SELECT * FROM ad_deliveries WHERE id=?", (target['delivery_id'],))
        if not delivery or delivery['status'] != 'active':
            return
        if kind == 'edit':
            old = json.loads(delivery['content'])
            replace = ((old['kind'] != content['kind'] and
                        (old['kind'] in ('text','sticker') or content['kind'] in ('text','sticker')))
                       or (old['kind']=='sticker' and
                           (old.get('file_id') != content.get('file_id') or
                            bool(old.get('text')) != bool(content.get('text')))))
            if replace:
                # Это именно замена, т.к. Telegram не редактирует стикеры.
                new_id, new_extra = await replace_ad_delivery(self.bot, delivery, content, buttons)
                self.db.execute("""UPDATE ad_deliveries
                    SET message_id=?,extra_message_id=?,content=?,buttons=?,last_error=''
                    WHERE id=?""", (new_id,new_extra,job['content'],job['buttons'],delivery['id']))
            else:
                await edit_ad_delivery(self.bot, delivery, content, buttons)
                self.db.execute("""UPDATE ad_deliveries
                    SET content=?,buttons=?,last_error='' WHERE id=?""",
                    (job['content'], job['buttons'], delivery['id']))
        else:
            # Удаляем сначала дополнительный текст, затем основное медиа/стикер.
            # При ограничении 48ч запись остаётся active для ручного удаления.
            if delivery['extra_message_id']:
                await _safe_delete(self.bot, chat_id, delivery['extra_message_id'])
            await _safe_delete(self.bot, chat_id, delivery['message_id'])
            self.db.execute("UPDATE ad_deliveries SET status='removed',last_error='' WHERE id=?", (delivery['id'],))
