import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()
_conn = sqlite3.connect(DATA_DIR / "bot.db", check_same_thread=False)
_conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, lang TEXT NOT NULL)")
_conn.execute(
    "CREATE TABLE IF NOT EXISTS media_cache ("
    "url TEXT NOT NULL, kind TEXT NOT NULL, file_id TEXT NOT NULL, "
    "title TEXT, performer TEXT, duration INTEGER DEFAULT 0, "
    "PRIMARY KEY (url, kind))"
)
_conn.commit()
_conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
_conn.commit()

# --- Migrations: allow NULL lang (track users before they pick a language),
# --- add joined_at + required_channels table.
def _migrate() -> None:
    with _lock:
        cols = {r[1]: r for r in _conn.execute("PRAGMA table_info(users)").fetchall()}
        if cols and cols.get("lang", (None, None, None, 1))[3] == 1:
            # lang is NOT NULL -> rebuild table as nullable with joined_at
            _conn.execute(
                "CREATE TABLE IF NOT EXISTS users_new "
                "(id INTEGER PRIMARY KEY, lang TEXT, "
                "joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            _conn.execute(
                "INSERT OR IGNORE INTO users_new (id, lang) SELECT id, lang FROM users"
            )
            _conn.execute("DROP TABLE users")
            _conn.execute("ALTER TABLE users_new RENAME TO users")
            _conn.commit()
        # add joined_at column if missing (fresh nullable table may lack it)
        cols = {r[1]: r for r in _conn.execute("PRAGMA table_info(users)").fetchall()}
        if "joined_at" not in cols:
            _conn.execute("ALTER TABLE users ADD COLUMN joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
            _conn.commit()
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS required_channels ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "chat_id INTEGER UNIQUE NOT NULL, "
            "title TEXT NOT NULL DEFAULT '', "
            "invite_link TEXT NOT NULL DEFAULT '')"
        )
        _conn.commit()


_migrate()


def get_lang(user_id: int) -> str | None:
    with _lock:
        row = _conn.execute("SELECT lang FROM users WHERE id=?", (user_id,)).fetchone()
    return row[0] if row else None


def set_lang(user_id: int, lang: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO users (id, lang) VALUES (?, ?) "
            "ON CONFLICT(id) DO UPDATE SET lang=excluded.lang",
            (user_id, lang),
        )
        _conn.commit()


def ensure_user(user_id: int) -> None:
    """Track a user even before they pick a language (lang stays NULL)."""
    with _lock:
        _conn.execute("INSERT OR IGNORE INTO users (id) VALUES (?)", (user_id,))
        _conn.commit()


def count_users() -> int:
    with _lock:
        row = _conn.execute("SELECT COUNT(*) FROM users").fetchone()
    return row[0] if row else 0


def count_users_with_lang() -> int:
    with _lock:
        row = _conn.execute("SELECT COUNT(*) FROM users WHERE lang IS NOT NULL").fetchone()
    return row[0] if row else 0


def get_all_user_ids() -> list[int]:
    with _lock:
        rows = _conn.execute("SELECT id FROM users").fetchall()
    return [r[0] for r in rows]


def get_media(url: str, kind: str) -> dict | None:
    with _lock:
        row = _conn.execute(
            "SELECT file_id, title, performer, duration FROM media_cache WHERE url=? AND kind=?",
            (url, kind),
        ).fetchone()
    if not row:
        return None
    return {"file_id": row[0], "title": row[1], "performer": row[2], "duration": row[3] or 0}


def put_media(url: str, kind: str, file_id: str, title: str | None = None,
              performer: str | None = None, duration: int = 0) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO media_cache (url, kind, file_id, title, performer, duration) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(url, kind) DO UPDATE SET file_id=excluded.file_id, "
            "title=excluded.title, performer=excluded.performer, duration=excluded.duration",
            (url, kind, file_id, title, performer, duration),
        )
        _conn.commit()


# ---------- required (force-sub) channels ----------

def get_channels() -> list[dict]:
    with _lock:
        rows = _conn.execute(
            "SELECT id, chat_id, title, invite_link FROM required_channels ORDER BY id"
        ).fetchall()
    return [{"id": r[0], "chat_id": r[1], "title": r[2], "invite_link": r[3]} for r in rows]


def add_channel(chat_id: int, title: str, invite_link: str) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO required_channels (chat_id, title, invite_link) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, invite_link=excluded.invite_link",
            (chat_id, title, invite_link),
        )
        _conn.commit()


def remove_channel(row_id: int) -> None:
    with _lock:
        _conn.execute("DELETE FROM required_channels WHERE id=?", (row_id,))
        _conn.commit()


# ---------- settings ----------

def get_setting(key: str) -> str | None:
    with _lock:
        row = _conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(key: str, value: str | None) -> None:
    with _lock:
        if value is None:
            _conn.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            _conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
        _conn.commit()


def get_cache_channel() -> str | None:
    return get_setting("cache_channel") or (os.getenv("CACHE_CHANNEL", "").strip() or None)
