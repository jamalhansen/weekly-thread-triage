import sqlite3
from pathlib import Path
from typing import List
from local_first_common import db
from .schema import ThreadRow

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS thread_triage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_section TEXT,
    thread_text TEXT NOT NULL,
    thread_type TEXT NOT NULL DEFAULT 'task',
    search_term TEXT,
    suggested_disposition TEXT,
    suggested_action TEXT,
    rationale TEXT,
    human_disposition TEXT,
    resurface_after TEXT,
    executed_at TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

def init_db(db_path: Path) -> None:
    """Initialize the thread_triage table and handle migrations."""
    db.init_db(db_path, _CREATE_TABLE)
    
    # Migration: add search_term if missing
    with db.get_db_cursor(db_path) as cur:
        if cur:
            try:
                cur.execute("ALTER TABLE thread_triage ADD COLUMN search_term TEXT")
                cur.connection.commit()
            except sqlite3.OperationalError:
                pass  # already exists

def write_rows(db_path: Path, rows: List[ThreadRow]) -> int:
    """Insert rows into thread_triage, skipping duplicates."""
    inserted = 0
    with db.get_db_cursor(db_path) as cur:
        if cur is None:
            return 0
        for row in rows:
            # Same-week dedup
            cur.execute(
                "SELECT id FROM thread_triage WHERE week = ? AND thread_text = ?",
                (row.week, row.thread_text),
            )
            if cur.fetchone():
                continue

            # Cross-week dedup
            cur.execute(
                """SELECT id FROM thread_triage
                   WHERE thread_text = ? AND human_disposition IS NOT NULL
                     AND human_disposition != 'defer' AND week != ?""",
                (row.thread_text, row.week),
            )
            if cur.fetchone():
                continue

            cur.execute(
                """INSERT INTO thread_triage
                   (week, source_file, source_section, thread_text, thread_type, search_term)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (row.week, row.source_file, row.source_section, row.thread_text, row.thread_type, row.search_term),
            )
            inserted += 1
        cur.connection.commit()
    return inserted

def build_context_payload(conn: sqlite3.Connection) -> str:
    """Build a compact context string to ground the LLM's classifications."""
    recent = conn.execute(
        """SELECT thread_text, human_disposition FROM thread_triage
           WHERE human_disposition IS NOT NULL
           ORDER BY created_at DESC LIMIT 10"""
    ).fetchall()

    lines = ["Recent dispositions (for context — avoid re-suggesting these):"]
    for text, disp in recent:
        lines.append(f"  [{disp}] {text[:60]}")

    return "\n".join(lines) if recent else ""
