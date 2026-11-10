"""Необязательно: отдельно запустить установку зависимостей."""
from bootstrap import ensure_dependencies

if __name__ == "__main__":
    if ensure_dependencies():
        print("Зависимости установлены. Можно запускать main.py.")
