"""Запуск бота на aiogram 3: настройки из переменных окружения или .env."""
import sys
from pathlib import Path

# При запуске из редактора Pydroid соседние модули должны быть доступны.
PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))


def launch():
    try:
        from bootstrap import ensure_config, ensure_dependencies
        if not ensure_dependencies() or not ensure_config():
            return
        import asyncio
        from runtime import run, configure_logging
        from settings import Settings
        cfg = Settings.load()
        configure_logging(cfg.bot_token)
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("Бот остановлен.")
    except ValueError as exc:
        print(f"Настройка: {exc}")
    except OSError as exc:
        print(f"Ошибка доступа к файлам/порту: {type(exc).__name__}. Проверь разрешения Pydroid и data/bot.log.")
    except Exception as exc:
        import logging
        from api import explain_error
        logging.getLogger(__name__).exception("Остановка бота")
        print(explain_error(exc))


if __name__ == "__main__":
    launch()
