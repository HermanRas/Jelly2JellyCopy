"""SQLite: settings and the source connection. The jobs table joins in phase 4.

Plain sqlite3, one short-lived connection per call: every query here is tiny,
and it keeps the db safe to touch from both request handlers and the
download worker without sharing a connection across threads.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- At most one row (id = 1): the app talks to one source server at a time.
-- The token is Fernet-encrypted; the password is never stored at all.
CREATE TABLE IF NOT EXISTS connection (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    url          TEXT NOT NULL,
    server_name  TEXT NOT NULL,
    server_id    TEXT NOT NULL,
    version      TEXT NOT NULL,
    user_name    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    token_enc    TEXT NOT NULL,
    connected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def db_path() -> Path:
    return config.DATA_DIR / "j2j.db"


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)


def get_setting(key: str, default: str | None = None) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def device_id() -> str:
    """Stable per install. Jellyfin keys sessions by device, so a fresh id per
    login would pile up a new "device" on the source server every time."""
    value = get_setting("device_id")
    if value is None:
        value = uuid.uuid4().hex
        set_setting("device_id", value)
    return value


def save_connection(*, url: str, server_name: str, server_id: str, version: str,
                    user_name: str, user_id: str, token_enc: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO connection "
            "(id, url, server_name, server_id, version, user_name, user_id, token_enc) "
            "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (url, server_name, server_id, version, user_name, user_id, token_enc))


def get_connection() -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM connection WHERE id = 1").fetchone()


def delete_connection() -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM connection")
