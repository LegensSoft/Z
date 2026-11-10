"""Локальная SQLite: файлы, публикации, спонсоры, выдачи и шаги админки."""
import json
import secrets
import sqlite3
from dataclasses import asdict
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA busy_timeout=10000;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, file_id TEXT NOT NULL,
                file_name TEXT NOT NULL, source_chat INTEGER NOT NULL,
                source_message INTEGER NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_chat, source_message)
            );
            CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                file_key TEXT NOT NULL REFERENCES files(id),
                external_url TEXT NOT NULL, content TEXT NOT NULL,
                channel_id INTEGER NOT NULL, message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'draft', revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sponsors (
                chat_id INTEGER PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                key TEXT PRIMARY KEY, state TEXT, data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_preferences (
                user_id INTEGER PRIMARY KEY, subscription_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS upload_inputs (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, file_key TEXT NOT NULL,
                PRIMARY KEY(chat_id, message_id)
            );
            CREATE TABLE IF NOT EXISTS pending_actions (
                user_id INTEGER PRIMARY KEY, token TEXT NOT NULL, action TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS post_publications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL REFERENCES posts(id),
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                removal_status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(channel_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_post_publications_post ON post_publications(post_id);
            CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status,created_at);
            CREATE INDEX IF NOT EXISTS idx_files_updated ON files(updated_at);

        """)
        columns = {r[1] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "status" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            # Старый мастер V2 имел другой порядок шагов; сохранённые публикации не затрагиваем.
            self.conn.execute("UPDATE sessions SET state=NULL,data='{}'")
        if "storage_removed" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN storage_removed INTEGER NOT NULL DEFAULT 0")
        if "delivery_text" not in columns:
            self.conn.execute("ALTER TABLE files ADD COLUMN delivery_text TEXT NOT NULL DEFAULT ''")
        # В V8 первичный ключ posts.id был также file ID: второй пост для
        # того же файла был физически невозможен. Меняем схему атомарно, НЕ
        # затрагивая существующие ID, черновики, публикации и file_id Telegram.
        # Отключаем FK только в рамках миграции, иначе DROP старой posts
        # каскадно нарушит связи с post_publications. Полная проверка FK в конце.
        post_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)")}
        if "file_key" not in post_cols:
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys=OFF")
            try:
                with self.conn:
                    self.conn.execute("""CREATE TABLE posts_migrating (
                        id TEXT PRIMARY KEY,
                        file_key TEXT NOT NULL REFERENCES files(id),
                        external_url TEXT NOT NULL, content TEXT NOT NULL,
                        channel_id INTEGER NOT NULL, message_id INTEGER,
                        status TEXT NOT NULL DEFAULT 'draft',
                        revision INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )""")
                    self.conn.execute("""INSERT INTO posts_migrating
                        (id,file_key,external_url,content,channel_id,message_id,status,revision,created_at)
                        SELECT id,id,external_url,content,channel_id,message_id,status,revision,created_at
                        FROM posts""")
                    self.conn.execute("DROP TABLE posts")
                    self.conn.execute("ALTER TABLE posts_migrating RENAME TO posts")
                    self.conn.execute("CREATE INDEX idx_posts_status ON posts(status,created_at)")
            finally:
                self.conn.execute("PRAGMA foreign_keys=ON")
            errors = self.conn.execute("PRAGMA foreign_key_check").fetchall()
            if errors:
                raise RuntimeError(f"Нарушены связи в базе после миграции posts: {errors[:3]}")
        # Forwarded posts cannot be edited and cannot carry inline keyboards.
        # Store the ID of the companion button message for every published copy.
        pub_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(post_publications)")}
        if "button_message_id" not in pub_cols:
            self.conn.execute("ALTER TABLE post_publications ADD COLUMN button_message_id INTEGER")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_file ON posts(file_key,created_at)")
        # Публикации из старых версий остаются в базе и автоматически
        # получают запись в новом реестре всех копий сообщения.
        self.conn.execute("""INSERT OR IGNORE INTO post_publications(post_id,channel_id,message_id)
            SELECT id,channel_id,message_id FROM posts WHERE message_id IS NOT NULL""")
        # Пользователи, с которыми бот уже общался, доступны из истории выдач.
        # Новые пользователи регистрируются ДО проверки подписки.
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                blocked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS ads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                buttons TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'draft',
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ad_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL REFERENCES ads(id),
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                extra_message_id INTEGER,
                content TEXT NOT NULL,
                buttons TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id,message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ad_deliveries ON ad_deliveries(ad_id,status);
            CREATE TABLE IF NOT EXISTS ad_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ad_id INTEGER NOT NULL REFERENCES ads(id),
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                buttons TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ad_job_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES ad_jobs(id),
                chat_id INTEGER NOT NULL,
                delivery_id INTEGER REFERENCES ad_deliveries(id),
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_ad_job_targets ON ad_job_targets(job_id,status);
            INSERT OR IGNORE INTO bot_users(user_id)
                SELECT DISTINCT user_id FROM downloads WHERE user_id>0;
            INSERT OR IGNORE INTO bot_users(user_id)
                SELECT DISTINCT user_id FROM pending_actions WHERE user_id>0;
        """)
        self.conn.commit()

    def upsert_user(self, user_id, username=''):
        self.execute("""INSERT INTO bot_users(user_id,username) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,last_seen=CURRENT_TIMESTAMP,blocked=0""",
            (user_id, username or ''))

    def save_ad(self, content, buttons, ad_id=None):
        payload = (json.dumps(content, ensure_ascii=False), json.dumps(buttons, ensure_ascii=False))
        if ad_id is None:
            cur = self.execute("INSERT INTO ads(title,content,buttons) VALUES(?,?,?)",
                ('Новая реклама', *payload))
            ad_id = cur.lastrowid
            self.execute("UPDATE ads SET title=? WHERE id=?", (f'Реклама №{ad_id}', ad_id))
        else:
            row = self.one("SELECT * FROM ads WHERE id=?", (ad_id,))
            if not row or row['status']=='deleted':
                raise ValueError('Реклама удалена или не найдена.')
            self.execute("""UPDATE ads SET content=?,buttons=?,revision=revision+1
                WHERE id=?""", (*payload, ad_id))
        return ad_id

    def ad(self, ad_id):
        row = self.one("SELECT * FROM ads WHERE id=?", (ad_id,))
        if row:
            row['content'] = json.loads(row['content'])
            row['buttons'] = json.loads(row['buttons'])
        return row

    def ad_job(self, job_id):
        return self.one("""SELECT j.*, 
            (SELECT COUNT(*) FROM ad_job_targets t WHERE t.job_id=j.id) total,
            (SELECT COUNT(*) FROM ad_job_targets t WHERE t.job_id=j.id AND t.status='done') done,
            (SELECT COUNT(*) FROM ad_job_targets t WHERE t.job_id=j.id AND t.status='failed') failed
            FROM ad_jobs j WHERE id=?""", (job_id,))

    def queue_ad_job(self, ad_id, kind):
        if kind not in ('send','edit','delete'):
            raise ValueError('Недопустимая операция с рекламой.')
        ad = self.ad(ad_id)
        if not ad:
            raise ValueError('Реклама не найдена.')
        if ad['status']=='deleted' and kind!='delete':
            raise ValueError('Реклама удалена.')
        if self.one("SELECT id FROM ad_jobs WHERE ad_id=? AND status IN ('pending','running')", (ad_id,)):
            raise ValueError('С рекламой уже выполняется операция. Дождись окончания и попробуй снова.')
        with self.conn:
            cur = self.conn.execute("INSERT INTO ad_jobs(ad_id,kind,content,buttons) VALUES(?,?,?,?)",
                (ad_id, kind, json.dumps(ad['content'], ensure_ascii=False),
                 json.dumps(ad['buttons'], ensure_ascii=False)))
            job_id = cur.lastrowid
            if kind=='send':
                self.conn.execute("""INSERT INTO ad_job_targets(job_id,chat_id)
                    SELECT ?,user_id FROM bot_users WHERE user_id>0 AND blocked=0""", (job_id,))
            else:
                self.conn.execute("""INSERT INTO ad_job_targets(job_id,chat_id,delivery_id)
                    SELECT ?,chat_id,id FROM ad_deliveries
                    WHERE ad_id=? AND status='active'""", (job_id,ad_id))
            if kind=='delete':
                self.conn.execute("UPDATE ads SET status='deleted' WHERE id=?", (ad_id,))
        return job_id

    def ad_stats(self, ad_id):
        return self.one("""SELECT
             (SELECT COUNT(*) FROM ad_deliveries WHERE ad_id=? AND status='active') active,
             (SELECT COUNT(*) FROM ad_deliveries WHERE ad_id=? AND status='removed') removed,
             (SELECT COUNT(*) FROM ad_jobs WHERE ad_id=?) jobs""",
             (ad_id,ad_id,ad_id))

    def ad_latest_job(self, ad_id):
        return self.one("SELECT id FROM ad_jobs WHERE ad_id=? ORDER BY id DESC LIMIT 1", (ad_id,))

    def execute(self, sql, params=()):
        with self.conn:
            return self.conn.execute(sql, params)

    def one(self, sql, params=()):
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def file(self, file_key, active=False):
        return self.one("SELECT * FROM files WHERE id=?" + (" AND status='active'" if active else ""), (file_key,))

    def reserve_upload(self, chat_id, message_id, file_id, name):
        """ID резервируется до отправки. Один update не создаёт два файла."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            previous = self.one("SELECT file_key FROM upload_inputs WHERE chat_id=? AND message_id=?", (chat_id, message_id))
            if previous:
                return self.file(previous["file_key"]), False
            row = self.one("SELECT value FROM counters WHERE name='file'")
            number = (row["value"] if row else 0) + 1
            # Только латинские буквы: никакой последовательной нумерации и цифр.
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"
            for _ in range(100):
                key = "".join(secrets.choice(alphabet) for _ in range(8))
                if not self.file(key):
                    break
            else:
                raise RuntimeError("Не удалось выделить уникальный буквенный ID.")
            self.conn.execute("INSERT INTO counters VALUES('file',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (number,))
            self.conn.execute("INSERT INTO files(id,file_id,file_name,source_chat,source_message,status) VALUES(?,?,?,?,?,'uploading')",
                              (key, file_id, name, 0, -number))
            self.conn.execute("INSERT INTO upload_inputs VALUES(?,?,?)", (chat_id, message_id, key))
        return self.file(key), True

    def finish_upload(self, key, file_id, chat_id, message_id):
        self.execute("UPDATE files SET file_id=?,source_chat=?,source_message=?,status='active',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (file_id, chat_id, message_id, key))

    def pending(self, user_id):
        row = self.one("SELECT * FROM pending_actions WHERE user_id=?", (user_id,))
        if row:
            row["action"] = json.loads(row["action"])
        return row

    def set_pending(self, user_id, token, action):
        self.execute("INSERT INTO pending_actions(user_id,token,action) VALUES(?,?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,action=excluded.action,updated_at=CURRENT_TIMESTAMP",
                     (user_id, token, json.dumps(action, ensure_ascii=False)))

    def register_file(self, key, file_id, name, chat, message, replace=False):
        old = self.file(key)
        if old and old["status"] == "deleted":
            raise ValueError("Этот файл удалён; старый ID не используется повторно.")
        if old and (old["source_chat"], old["source_message"]) != (chat, message) and not replace:
            raise ValueError(f"ID {key} уже занят другим сообщением. Используй «Заменить файл».")
        previous = self.one("SELECT id FROM files WHERE source_chat=? AND source_message=?", (chat, message))
        if previous and previous["id"] != key:
            raise ValueError("Нельзя менять id: у зарегистрированного сообщения. Добавь новый файл с новым ID.")
        self.execute("""INSERT INTO files(id,file_id,file_name,source_chat,source_message)
            VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            file_id=excluded.file_id,file_name=excluded.file_name,
            source_chat=excluded.source_chat,source_message=excluded.source_message,
            updated_at=CURRENT_TIMESTAMP""", (key, file_id, name, chat, message))

    def post(self, key, active=False):
        """Пост определяется своим ID, не ID документа. У одного файла их много."""
        suffix = " AND status='published'" if active else ""
        row = self.one("SELECT * FROM posts WHERE id=?" + suffix, (key,))
        if row:
            row["content"] = json.loads(row["content"])
        return row

    def posts_for_file(self, file_key, include_deleted=False):
        return self.all("SELECT id,file_key,status,created_at FROM posts WHERE file_key=? "
                        + ("" if include_deleted else "AND status!='deleted' ")
                        + "ORDER BY created_at DESC,rowid DESC", (file_key,))

    def create_post(self, file_key, url, content, channel_id, status="draft"):
        """Создаёт новый НЕЗАВИСИМЫЙ пост с тем же файлом, без повторной загрузки."""
        if not self.file(file_key, active=True):
            raise ValueError("Файл не найден или удалён.")
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        with self.conn:
            for _ in range(100):
                post_id = "P" + "".join(secrets.choice(alphabet) for _ in range(9))
                if not self.post(post_id) and not self.file(post_id):
                    self.conn.execute("""INSERT INTO posts
                        (id,file_key,external_url,content,channel_id,status)
                        VALUES(?,?,?,?,?,?)""",
                        (post_id, file_key, url, json.dumps(content, ensure_ascii=False),
                         channel_id, status))
                    return post_id
        raise RuntimeError("Не удалось назначить отдельный ID публикации.")

    def save_post(self, key, url, content, channel_id, message_id=None, status="draft", file_key=None):
        """Совместимость с уже используемыми явными ID старых постов и тестами."""
        file_key = file_key or key
        self.execute("""INSERT INTO posts
            (id,file_key,external_url,content,channel_id,message_id,status)
            VALUES(?,?,?,?,?,?,?)""",
            (key, file_key, url, json.dumps(content, ensure_ascii=False),
             channel_id, message_id, status))
        if message_id is not None:
            self.execute("""INSERT OR IGNORE INTO post_publications
                (post_id,channel_id,message_id) VALUES(?,?,?)""", (key, channel_id, message_id))

    def update_post(self, key, url, content):
        self.execute("UPDATE posts SET external_url=?,content=?,revision=revision+1 WHERE id=?",
                     (url, json.dumps(content, ensure_ascii=False), key))

    def publications(self, key, include_removed=False):
        return self.all("SELECT * FROM post_publications WHERE post_id=? "
                        + ("" if include_removed else "AND removal_status='active' ")
                        + "ORDER BY id", (key,))

    def record_publication(self, key, channel_id, message_id, button_message_id=None):
        # Каждое нажатие «Опубликовать повторно» создаёт отдельную копию.
        with self.conn:
            self.conn.execute("""INSERT OR IGNORE INTO post_publications
                (post_id,channel_id,message_id,button_message_id) VALUES(?,?,?,?)""",
                (key, channel_id, message_id, button_message_id))
            self.conn.execute("""UPDATE posts SET status='published',message_id=?
                WHERE id=?""", (message_id, key))

    def replace_publication(self, pub_id, post_id, channel_id, message_id, button_message_id):
        with self.conn:
            self.conn.execute("""UPDATE post_publications
                SET message_id=?,button_message_id=?,removal_status='active'
                WHERE id=?""", (message_id, button_message_id, pub_id))
            self.conn.execute("UPDATE posts SET message_id=? WHERE id=?",
                              (message_id, post_id))

    def set_publication_button(self, pub_id, button_message_id):
        self.execute("UPDATE post_publications SET button_message_id=? WHERE id=?",
                     (button_message_id, pub_id))

    def mark_publication_removed(self, publication_id, status):
        if status not in ('deleted', 'disabled', 'failed'):
            raise ValueError("Некорректный статус публикации")
        self.execute("UPDATE post_publications SET removal_status=? WHERE id=?",
                     (status, publication_id))

    def sponsors(self):
        return self.all("SELECT * FROM sponsors ORDER BY title,chat_id")

    def update_delivery_text(self, key, value):
        if not self.file(key, active=True):
            raise ValueError("Файл удалён или не найден.")
        self.execute("UPDATE files SET delivery_text=?,updated_at=CURRENT_TIMESTAMP WHERE id=?", (value, key))

    def templates(self):
        return self.all("SELECT id,name,body FROM delivery_templates ORDER BY name COLLATE NOCASE,id")

    def template(self, template_id):
        return self.one("SELECT id,name,body FROM delivery_templates WHERE id=?", (template_id,))

    def admin_subscription_enabled(self, user_id):
        row = self.one("SELECT subscription_enabled FROM admin_preferences WHERE user_id=?", (user_id,))
        return True if row is None else bool(row["subscription_enabled"])

    def set_admin_subscription_enabled(self, user_id, enabled):
        self.execute("INSERT INTO admin_preferences(user_id,subscription_enabled) VALUES(?,?) "
                     "ON CONFLICT(user_id) DO UPDATE SET subscription_enabled=excluded.subscription_enabled",
                     (user_id, 1 if enabled else 0))

    def close(self):
        self.conn.close()


class SQLiteStorage(BaseStorage):
    """Сохраняет незаконченные шаги, включая предпросмотр, после перезапуска."""
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _key(key: StorageKey):
        return json.dumps(asdict(key), sort_keys=True)

    async def set_state(self, key: StorageKey, state=None):
        state = state.state if isinstance(state, State) else state
        self.db.execute("INSERT INTO sessions(key,state) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET state=excluded.state",
                        (self._key(key), state))

    async def get_state(self, key: StorageKey):
        row = self.db.one("SELECT state FROM sessions WHERE key=?", (self._key(key),))
        return row["state"] if row else None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]):
        if not isinstance(data, Mapping):
            raise TypeError("FSM data must be a mapping")
        self.db.execute("INSERT INTO sessions(key,data) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                        (self._key(key), json.dumps(dict(data), ensure_ascii=False)))

    async def get_data(self, key: StorageKey):
        row = self.db.one("SELECT data FROM sessions WHERE key=?", (self._key(key),))
        return json.loads(row["data"]) if row else {}

    async def close(self):
        # Соединение общее с приложением; закрывается в main.py после остановки Dispatcher.
        pass
