import sqlite3
from pathlib import Path
from typing import List
from local_first_common import db
from .schema import ThreadRow

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
                   (week, source_file, source_section, thread_text, thread_type)
                   VALUES (?, ?, ?, ?, ?)""",
                (row.week, row.source_file, row.source_section, row.thread_text, row.thread_type),
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
