"""Запуск из папки с ботом. Подходит для любой папки в Pydroid 3."""
import runpy
import sys
from pathlib import Path

folder = Path(__file__).resolve().parent
target = folder / "main.py"
if target.is_file():
    sys.path.insert(0, str(folder))
    runpy.run_path(str(target), run_name="__main__")
else:
    print(f"main.py не найден рядом с START_PYDROID.py: {folder}")
    print("Полностью распакуй архив в одну папку и открой из неё main.py в Pydroid 3.")
