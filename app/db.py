"""SQLite: settings, the source connection and the download queue.

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

-- One row per file. Queuing a season expands to one job per episode at
-- queue time, so progress, retries and skips are per file. Paths are fixed
-- at queue time too: changing the destination later never moves a job.
CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id      TEXT NOT NULL,     -- jobs only run against the server they came from
    source_item_id TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('episode', 'movie', 'track', 'subtitle')),
    display_name   TEXT NOT NULL,
    dest_root      TEXT NOT NULL,
    target_path    TEXT NOT NULL,
    download_path  TEXT NOT NULL,     -- source API path, built by the app, never by the browser
    size           INTEGER,           -- from MediaSources[0].Size; NULL when unknown (subtitles)
    bytes_done     INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
                     ('queued', 'downloading', 'paused', 'done', 'skipped', 'failed', 'cancelled')),
    error          TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    not_before     REAL NOT NULL DEFAULT 0,   -- retry backoff, unix time
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id);
"""

ACTIVE = ("queued", "downloading", "paused")
FINISHED = ("done", "skipped", "cancelled")


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


# ---- jobs --------------------------------------------------------------------

JOB_FIELDS = ("server_id", "source_item_id", "kind", "display_name", "dest_root",
              "target_path", "download_path", "size")


def add_jobs(jobs: list[dict]) -> tuple[int, int]:
    """Insert jobs, skipping any whose source item or target file is already
    queued, downloading or paused. Returns (added, skipped as duplicates)."""
    added = dupes = 0
    with closing(_connect()) as conn, conn:
        for job in jobs:
            exists = conn.execute(
                f"SELECT 1 FROM jobs WHERE status IN {ACTIVE} AND "
                "((server_id = ? AND source_item_id = ? AND kind = ?) OR target_path = ?) LIMIT 1",
                (job["server_id"], job["source_item_id"], job["kind"], job["target_path"])).fetchone()
            if exists:
                dupes += 1
                continue
            conn.execute(f"INSERT INTO jobs ({', '.join(JOB_FIELDS)}) VALUES ({', '.join('?' * len(JOB_FIELDS))})",
                         tuple(job.get(f) for f in JOB_FIELDS))
            added += 1
    return added, dupes


def get_job(job_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def update_job(job_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    if fields.get("status") in FINISHED + ("failed",):
        cols += ", finished_at = datetime('now')"
    with closing(_connect()) as conn, conn:
        conn.execute(f"UPDATE jobs SET {cols}, updated_at = datetime('now') WHERE id = ?",
                     (*fields.values(), job_id))


def next_job(server_id: str, exclude: set[int], now: float) -> sqlite3.Row | None:
    # No "NOT IN (NULL)" shortcut for an empty set: in SQL that is never true.
    skip = f"AND id NOT IN ({','.join('?' * len(exclude))}) " if exclude else ""
    with closing(_connect()) as conn:
        return conn.execute(
            f"SELECT * FROM jobs WHERE status = 'queued' AND server_id = ? AND not_before <= ? "
            f"{skip}ORDER BY id LIMIT 1",
            (server_id, now, *exclude)).fetchone()


def list_jobs(limit: int = 300) -> list[sqlite3.Row]:
    # Active work first (in queue order), then the most recently finished.
    with closing(_connect()) as conn:
        return conn.execute(
            "SELECT * FROM jobs ORDER BY "
            "CASE status WHEN 'downloading' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 "
            "WHEN 'failed' THEN 3 ELSE 4 END, "
            "CASE WHEN status IN ('downloading', 'queued', 'paused') THEN id ELSE -id END "
            "LIMIT ?", (limit,)).fetchall()


def job_counts() -> dict[str, int]:
    with closing(_connect()) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def active_roots() -> list[str]:
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT DISTINCT dest_root FROM jobs WHERE status IN {ACTIVE}").fetchall()
    return [r["dest_root"] for r in rows]


def set_status_where(new: str, old: tuple[str, ...], **extra) -> int:
    sets = ", ".join(["status = ?"] + [f"{k} = ?" for k in extra])
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            f"UPDATE jobs SET {sets}, updated_at = datetime('now') "
            f"WHERE status IN ({','.join('?' * len(old))})", (new, *extra.values(), *old))
        return cur.rowcount


def clear_finished() -> int:
    with closing(_connect()) as conn, conn:
        return conn.execute(
            f"DELETE FROM jobs WHERE status IN ({','.join('?' * len(FINISHED))})", FINISHED).rowcount
