"""Настройки из окружения; локальный .env используется как запасной источник."""
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values, set_key

ROOT = Path(__file__).resolve().parent


def read_environment(path=None):
    """Окружение процесса имеет приоритет. .env не меняет os.environ."""
    path = Path(path or ROOT / ".env")
    values = dict(dotenv_values(path, encoding="utf-8-sig", interpolate=False)) if path.exists() else {}
    values.update(os.environ)
    # Алиасы одного параметра тоже подчиняются приоритету окружения.
    for names in (("BOT_TOKEN", "TELEGRAM_BOT_TOKEN"), ("ADMIN_ID", "ADMIN_IDS")):
        if any(name in os.environ for name in names):
            for name in names:
                values.pop(name, None)
            values.update({name: os.environ[name] for name in names if name in os.environ})
    return values


def read_bot_token(path=None):
    values = read_environment(path)
    token = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    if token is None:
        token = values.get("BOT_TOKEN") or values.get("TELEGRAM_BOT_TOKEN") or ""
    token = str(token).strip()
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Задай BOT_TOKEN или TELEGRAM_BOT_TOKEN в переменных окружения либо в .env.")
    return token


def write_environment(path, values):
    """Атомарная запись .env с сохранением комментариев и остальных переменных."""
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        temporary.write_text(path.read_text(encoding="utf-8-sig") if path.exists() else "", encoding="utf-8")
        for key, value in values.items():
            set_key(str(temporary), key, str(value), quote_mode="always", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def channel_ref(value):
    if value in (None, "", 0, "0"):
        return 0
    if type(value) is int:
        if value >= 0:
            raise ValueError("ID канала/группы должен быть отрицательным. У обычной группы он может начинаться без -100.")
        return value
    if not isinstance(value, str):
        raise ValueError("Укажи отрицательный ID чата, @username или ссылку на пост.")
    value = value.strip()
    if re.fullmatch(r"-\d+", value):
        return int(value)
    if value.startswith(("https://", "http://", "t.me/", "telegram.me/")):
        url = urlsplit(value if "://" in value else "https://" + value)
        if url.hostname not in ("t.me", "telegram.me"):
            raise ValueError("Нужна ссылка Telegram-канала.")
        parts = url.path.strip("/").split("/")
        if parts[0] == "c" and len(parts) >= 2 and parts[1].isdigit():
            return int("-100" + parts[1])
        if parts[0] == "s" and len(parts) >= 2:
            parts = parts[1:]
        if parts[0].startswith("+") or parts[0] == "joinchat":
            raise ValueError("По приватной ссылке приглашения нельзя узнать ID. Перешли сообщение из канала.")
        value = "@" + parts[0]
    if re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{3,31}", value):
        return value
    raise ValueError("Пришли @username, ссылку публичного поста, ID -100… или перешли сообщение канала.")


@dataclass
class Settings:
    bot_token: str = field(repr=False)
    admin_ids: frozenset[int]
    storage_channel_id: int | str = 0
    main_channel_id: int | str = 0
    channel_custom_emoji: bool = True
    download_cooldown_seconds: int = 2
    path: Path = field(default_factory=lambda: ROOT / ".env", repr=False)
    warnings: list[str] = field(default_factory=list, repr=False)
    external_channels: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def load(cls, path=None):
        path = Path(path or ROOT / ".env")
        raw = read_environment(path)
        token = read_bot_token(path)
        # Бот сохраняет исходную модель одного владельца.
        admin = str(raw.get("ADMIN_ID") or raw.get("ADMIN_IDS") or "").strip()
        if not re.fullmatch(r"[1-9]\d*", admin):
            raise ValueError("Задай ADMIN_ID: один положительный Telegram ID владельца (ADMIN_IDS — алиас).")
        values, warnings = {}, []
        for key in ("storage_channel_id", "main_channel_id"):
            try:
                values[key] = channel_ref(raw.get(key.upper()))
            except ValueError as exc:
                values[key] = 0
                warnings.append(f"{key.upper()}: {exc}")
        if values["storage_channel_id"] and values["storage_channel_id"] == values["main_channel_id"]:
            values["main_channel_id"] = 0
            warnings.append("Каналы файлов и публикаций должны быть разными. Основной канал нужно привязать заново.")
        try:
            cooldown = max(1, min(int(raw.get("DOWNLOAD_COOLDOWN_SECONDS", 2)), 60))
        except (TypeError, ValueError):
            cooldown = 2
        emoji = str(raw.get("CHANNEL_CUSTOM_EMOJI", "true")).strip().lower() in ("true", "1", "yes", "on")
        external = frozenset(k for k in ("STORAGE_CHANNEL_ID", "MAIN_CHANNEL_ID") if k in os.environ)
        return cls(token, frozenset({int(admin)}), **values, channel_custom_emoji=emoji,
                   download_cooldown_seconds=cooldown, path=path, warnings=warnings, external_channels=external)

    def save_channels(self, storage=None, main=None):
        new_storage = self.storage_channel_id if storage is None else channel_ref(storage)
        new_main = self.main_channel_id if main is None else channel_ref(main)
        if new_storage and new_main and new_storage == new_main:
            raise ValueError("Хранилище файлов и основной канал должны быть разными.")
        changes = {}
        for name, new, old in (("STORAGE_CHANNEL_ID", new_storage, self.storage_channel_id),
                                ("MAIN_CHANNEL_ID", new_main, self.main_channel_id)):
            if new != old:
                if name in self.external_channels:
                    raise ValueError(f"{name} задан в окружении процесса. Измени эту переменную и перезапусти бота.")
                changes[name] = new
        if changes:
            write_environment(self.path, changes)
        self.storage_channel_id, self.main_channel_id = new_storage, new_main
