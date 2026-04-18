"""Tests for write_rows: insertion, deduplication, and cross-week logic."""

import sqlite3
from pathlib import Path

from triage.schema import ThreadRow
from triage.db import write_rows


def make_db(tmp_path: Path) -> Path:
    db = tmp_path / "local-first.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE thread_triage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            week TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_section TEXT,
            thread_text TEXT NOT NULL,
            thread_type TEXT,
            search_term TEXT,
            suggested_disposition TEXT,
            suggested_action TEXT,
            rationale TEXT,
            human_disposition TEXT,
            resurface_after TEXT,
            executed_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    return db


class TestWriteRows:
    def test_inserts_new_rows(self, tmp_path):
        db = make_db(tmp_path)
        rows = [ThreadRow("2026-W11", "a.md", "Morning Pages", "Fix scanner bug here", "task")]
        inserted = write_rows(db, rows)
        assert inserted == 1

        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM thread_triage").fetchone()[0]
        conn.close()
        assert count == 1

    def test_skips_duplicate_for_same_week(self, tmp_path):
        db = make_db(tmp_path)
        rows = [ThreadRow("2026-W11", "a.md", None, "Fix scanner", "task")]
        write_rows(db, rows)
        inserted = write_rows(db, rows)  # second write
        assert inserted == 0

    def test_allows_same_text_different_week_when_not_actioned(self, tmp_path):
        """Same text in different weeks is allowed when not yet actioned."""
        db = make_db(tmp_path)
        r1 = [ThreadRow("2026-W11", "a.md", None, "Fix scanner", "task")]
        r2 = [ThreadRow("2026-W12", "a.md", None, "Fix scanner", "task")]
        assert write_rows(db, r1) == 1
        assert write_rows(db, r2) == 1

    def test_skips_previously_actioned_in_different_week(self, tmp_path):
        """If text was actioned (human_disposition set) in a prior week, don't insert again."""
        db = make_db(tmp_path)

        # Insert W11 row and mark it as actioned
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage (week, source_file, thread_text, thread_type, human_disposition)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-W11", "a.md", "Fix the scanner bug", "task", "close"),
        )
        conn.commit()
        conn.close()

        # Attempt to insert same text for W12 — should be skipped
        r2 = [ThreadRow("2026-W12", "a.md", None, "Fix the scanner bug", "task")]
        inserted = write_rows(db, r2)
        assert inserted == 0

    def test_allows_same_text_next_week_if_not_yet_actioned(self, tmp_path):
        """If prior week's row has no human_disposition, allow re-insertion next week."""
        db = make_db(tmp_path)

        # Insert W11 row — no human_disposition (still pending)
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage (week, source_file, thread_text, thread_type)
               VALUES (?, ?, ?, ?)""",
            ("2026-W11", "a.md", "Fix the scanner bug", "task"),
        )
        conn.commit()
        conn.close()

        # W12 insert of the same text should succeed (not actioned yet)
        r2 = [ThreadRow("2026-W12", "a.md", None, "Fix the scanner bug", "task")]
        inserted = write_rows(db, r2)
        assert inserted == 1

    def test_deferred_rows_resurface_next_week(self, tmp_path):
        """Deferred rows are not blocked — they resurface each week until actioned differently."""
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage (week, source_file, thread_text, thread_type, human_disposition)
               VALUES (?, ?, ?, ?, ?)""",
            ("2026-W11", "a.md", "Worth revisiting this idea later", "thought", "defer"),
        )
        conn.commit()
        conn.close()

        r2 = [ThreadRow("2026-W12", "a.md", None, "Worth revisiting this idea later", "thought")]
        inserted = write_rows(db, r2)
        assert inserted == 1

    def test_sync_removes_edit_ghosts(self, tmp_path):
        """If a thought is edited in the file, the old version should be removed from DB."""
        db = make_db(tmp_path)
        
        # Initial scan: "Thought A"
        r1 = [ThreadRow("2026-W11", "a.md", None, "Thought A", "thought")]
        write_rows(db, r1)
        
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM thread_triage").fetchone()[0] == 1
        conn.close()
        
        # User edits "Thought A" to "Thought A edited"
        r2 = [ThreadRow("2026-W11", "a.md", None, "Thought A edited", "thought")]
        write_rows(db, r2)
        
        # DB should now only have "Thought A edited"
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT thread_text FROM thread_triage").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Thought A edited"

    def test_sync_preserves_actioned_ghosts(self, tmp_path):
        """If a thought is removed from the file but has a human_disposition, keep it in DB."""
        db = make_db(tmp_path)
        
        # Initial scan and user dispositions it
        r1 = [ThreadRow("2026-W11", "a.md", None, "Thought A", "thought")]
        write_rows(db, r1)
        
        conn = sqlite3.connect(db)
        conn.execute("UPDATE thread_triage SET human_disposition = 'capture' WHERE thread_text = 'Thought A'")
        conn.commit()
        conn.close()
        
        # User removes "Thought A" from the file (empty list for this file)
        # Note: In reality, other thoughts might remain, but for this test we pass empty
        write_rows(db, []) # No rows to sync
        
        # DB should still have "Thought A" because it has a human_disposition
        conn = sqlite3.connect(db)
        assert conn.execute("SELECT COUNT(*) FROM thread_triage").fetchone()[0] == 1
        conn.close()
