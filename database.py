"""SQLite persistence layer for licenses. Standard library only."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS licenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    key          TEXT    NOT NULL UNIQUE,
    status       TEXT    NOT NULL DEFAULT 'UNUSED',
    created_at   TEXT    NOT NULL,
    activated_at TEXT,
    expires_at   TEXT,
    user_id      INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_licenses_key ON licenses(key);
CREATE INDEX IF NOT EXISTS idx_licenses_user_id ON licenses(user_id);
CREATE INDEX IF NOT EXISTS idx_licenses_status ON licenses(status);

CREATE TABLE IF NOT EXISTS license_notifications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    license_key  TEXT    NOT NULL,
    milestone    TEXT    NOT NULL,
    user_id      INTEGER NOT NULL,
    sent_at      TEXT    NOT NULL,
    UNIQUE (license_key, milestone)
);
CREATE INDEX IF NOT EXISTS idx_license_notifications_user
    ON license_notifications(user_id);

CREATE TABLE IF NOT EXISTS folders (
    folder_id   INTEGER PRIMARY KEY,
    text        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);
"""


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def execute(sql: str, params: tuple[Any, ...] = ()) -> int:
    with connect() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount


def fetch_one(sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(sql, params).fetchall()


def save_folder(folder_id: int, text: str, timestamp: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO folders (folder_id, text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(folder_id) DO UPDATE SET text = excluded.text, "
            "updated_at = excluded.updated_at",
            (folder_id, text, timestamp, timestamp),
        )


def get_folder(folder_id: int) -> str | None:
    row = fetch_one("SELECT text FROM folders WHERE folder_id = ?", (folder_id,))
    return str(row["text"]) if row else None


def delete_folder(folder_id: int) -> bool:
    return execute("DELETE FROM folders WHERE folder_id = ?", (folder_id,)) > 0
