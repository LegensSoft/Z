"""FlyerHubs required-subscription API. No credentials in logs or errors."""
import asyncio
import aiohttp

API = 'https://api.flyerhubs.com'

class FlyerError(Exception):
    pass

class Flyer:
    def __init__(self, key, session=None):
        self.key, self.session = key.strip(), session

    async def request(self, path, data):
        if not self.key:
            raise FlyerError('Не задан FLYER_BOT_KEY')
        async def send(session):
            try:
                async with session.post(API + path, json={'key': self.key, **data}) as response:
                    if response.status != 200:
                        raise FlyerError('FlyerHubs: HTTP ' + str(response.status))
                    result = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise FlyerError('Ошибка сети или ответа FlyerHubs') from exc
            if not isinstance(result, dict):
                raise FlyerError('FlyerHubs вернул неверный формат ответа')
            if result.get('error') or result.get('warning'):
                raise FlyerError('FlyerHubs отклонил запрос. Проверьте ключ и статус бота в кабинете.')
            return result
        if self.session is not None:
            return await send(self.session)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12, connect=5)) as session:
            return await send(session)

    async def check(self, user_id, language='ru'):
        result = await self.request('/check', {'user_id': user_id, 'language_code': language or 'ru'})
        if type(result.get('skip')) is not bool:
            raise FlyerError('FlyerHubs /check: отсутствует логический skip')
        return result['skip']

    async def get_me(self):
        result = await self.request('/get_me', {})
        if result.get('type') != 'sub' or result.get('status') is not True:
            raise FlyerError('Нужен активный бот типа sub в FlyerHubs')
        return result
