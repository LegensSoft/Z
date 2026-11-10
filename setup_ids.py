"""Помощник получения ID. Не запускай одновременно с main.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def launch():
    from bootstrap import ensure_dependencies
    if not ensure_dependencies():
        return
    import asyncio
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import Message, MessageOriginChannel
    from settings import read_bot_token

    async def helper():
        token = read_bot_token()
        bot = Bot(token=token)
        dp = Dispatcher()

        @dp.message(F.chat.type == "private")
        async def private(message: Message):
            text = f"Твой Telegram ID: {message.from_user.id}"
            if isinstance(message.forward_origin, MessageOriginChannel):
                text += f"\nКанал: {message.forward_origin.chat.title}\nID канала: {message.forward_origin.chat.id}"
            await message.answer(text)

        @dp.channel_post()
        async def channel(message: Message):
            print(f"Канал {message.chat.title}: {message.chat.id}")

        try:
            me = await bot.get_me()
            print(f"Напиши @{me.username}, чтобы узнать свой ID. Перешли пост канала для ID канала.")
            print("После получения ID останови этот помощник и запускай main.py.")
            await bot.delete_webhook(drop_pending_updates=False)
            await dp.start_polling(bot, handle_signals=False)
        finally:
            await bot.session.close()

    try:
        asyncio.run(helper())
    except KeyboardInterrupt:
        print("Помощник остановлен.")
    except Exception as exc:
        from api import explain_error
        print(explain_error(exc))


if __name__ == "__main__":
    launch()
