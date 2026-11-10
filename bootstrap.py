"""Первый запуск без внешних импортов: установка и настройка в Pydroid 3."""
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def ensure_dependencies():
    if sys.version_info < (3, 10):
        print("Нужен Python 3.10+. Обнови Pydroid 3.")
        return False
    try:
        aiogram = importlib.import_module("aiogram")
        importlib.import_module("aiohttp")
        importlib.import_module("pydantic")
        importlib.import_module("dotenv")
        if aiogram.__version__ != "3.31.0":
            raise ImportError("Нужна версия aiogram из requirements.txt")
        return True
    except (ImportError, ModuleNotFoundError):
        print("Устанавливаю зависимости бота. Нужен интернет; дождись завершения…", flush=True)
    command = [sys.executable, "-m", "pip", "install", "--prefer-binary", "-r", str(ROOT / "requirements.txt")]
    log_path = ROOT / "install.log"
    try:
        with log_path.open("w", encoding="utf-8") as output:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", bufsize=1)
            for line in process.stdout:
                print(line, end="", flush=True)
                output.write(line)
            code = process.wait()
    except OSError as exc:
        print(f"Не удалось запустить pip: {type(exc).__name__}. Установи requirements.txt через Pip в Pydroid.")
        return False
    if code:
        print("Установка не завершена. Полная причина в install.log рядом с main.py.")
        print("Если ошибка связана с aiohttp/pydantic-core, нужны совместимые Android-пакеты для твоего Pydroid.")
        return False
    importlib.invalidate_caches()
    print("Зависимости установлены. Запусти main.py ещё раз — это обновит загруженные модули Python.")
    return False


def ensure_config():
    """Проверка без вопросов в stdin, токенов в JSON и автоматического восстановления ID."""
    from settings import Settings
    try:
        Settings.load(ROOT / ".env")
    except (ValueError, OSError) as exc:
        print(f"Настройка: {exc}")
        print("Заполни .env по .env.example или задай переменные окружения, затем запусти main.py.")
        return False
    return True
