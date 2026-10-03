import os
import sqlite3
import threading
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()
_conn = sqlite3.connect(DATA_DIR / "bot.db", check_same_thread=False)
_conn.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, lang TEXT NOT NULL)")
_conn.commit()


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
