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
    """Sync rows into thread_triage for the scanned files.
    
    Implements a 'Sync' approach:
    1. For every (week, source_file) present in 'rows', delete existing rows where human_disposition IS NULL
       AND the text is NOT in the current scan (this cleans up 'edit ghosts').
    2. Insert new rows if they don't already exist in the database (this handles re-scans).
    """
    if not rows:
        return 0

    inserted = 0
    path = Path(db_path).expanduser()
    conn = sqlite3.connect(str(path))
    try:
        cur = conn.cursor()
        
        # 1. Group rows by (week, source_file) to manage deletes per-file
        files_to_sync = {}
        for row in rows:
            key = (row.week, row.source_file)
            if key not in files_to_sync:
                files_to_sync[key] = set()
            files_to_sync[key].add(row.thread_text)
        
        # 2. For each file/week, delete orphaned un-dispositioned rows
        for (week, source_file), current_texts in files_to_sync.items():
            # Find all IDs for this file/week that are un-dispositioned
            cur.execute(
                "SELECT id, thread_text FROM thread_triage WHERE week = ? AND source_file = ? AND human_disposition IS NULL",
                (week, source_file)
            )
            existing = cur.fetchall()
            for db_id, db_text in existing:
                if db_text not in current_texts:
                    # This item was edited or deleted in the source note
                    cur.execute("DELETE FROM thread_triage WHERE id = ?", (db_id,))

        # 3. Insert new items
        for row in rows:
            # Same-week check: skip if identical text exists in this file/week
            cur.execute(
                "SELECT id FROM thread_triage WHERE week = ? AND source_file = ? AND thread_text = ?",
                (row.week, row.source_file, row.thread_text),
            )
            if cur.fetchone():
                continue

            # Cross-week check: skip if this text was ALREADY actioned (not 'defer') in another week
            # This respects the existing behavior where 'defer' items resurface.
            cur.execute(
                """SELECT id FROM thread_triage 
                   WHERE thread_text = ? AND human_disposition IS NOT NULL 
                   AND human_disposition != 'defer'""",
                (row.thread_text,)
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
            
        conn.commit()
    finally:
        conn.close()
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
