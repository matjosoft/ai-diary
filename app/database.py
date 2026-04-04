import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE UNIQUE NOT NULL,
    audio_files TEXT NOT NULL DEFAULT '[]',
    transcription TEXT NOT NULL DEFAULT '',
    summary TEXT,
    mood TEXT,
    mood_score INTEGER,
    events TEXT NOT NULL DEFAULT '[]',
    people TEXT NOT NULL DEFAULT '[]',
    planned_actions TEXT NOT NULL DEFAULT '[]',
    topics TEXT NOT NULL DEFAULT '[]',
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_type TEXT NOT NULL,
    period_key TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL,
    topics TEXT NOT NULL DEFAULT '[]',
    people TEXT NOT NULL DEFAULT '[]',
    mood_avg REAL,
    entry_count INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT (datetime('now')),
    updated_at DATETIME NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    date,
    transcription,
    summary,
    events,
    people,
    topics,
    planned_actions,
    content='entries',
    content_rowid='id'
);

-- Triggers to keep FTS index in sync with entries table
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, date, transcription, summary, events, people, topics, planned_actions)
    VALUES (new.id, new.date, new.transcription, new.summary, new.events, new.people, new.topics, new.planned_actions);
END;

CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, date, transcription, summary, events, people, topics, planned_actions)
    VALUES ('delete', old.id, old.date, old.transcription, old.summary, old.events, old.people, old.topics, old.planned_actions);
END;

CREATE TRIGGER IF NOT EXISTS entries_au AFTER UPDATE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, date, transcription, summary, events, people, topics, planned_actions)
    VALUES ('delete', old.id, old.date, old.transcription, old.summary, old.events, old.people, old.topics, old.planned_actions);
    INSERT INTO entries_fts(rowid, date, transcription, summary, events, people, topics, planned_actions)
    VALUES (new.id, new.date, new.transcription, new.summary, new.events, new.people, new.topics, new.planned_actions);
END;
"""


def get_db_path() -> Path:
    return settings.database_path


def init_db():
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)


def backfill_fts():
    """Rebuild FTS index from entries table. Safe to run on every startup."""
    with get_connection() as conn:
        conn.execute("INSERT INTO entries_fts(entries_fts) VALUES('rebuild')")


@contextmanager
def get_connection():
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
