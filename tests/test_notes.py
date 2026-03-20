"""Tests for create_capture_note, append_task, and run_act."""

import sqlite3
from datetime import date
from pathlib import Path

from triage.logic import append_task, create_capture_note, run_act


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


class TestCreateCaptureNote:
    def test_creates_file_in_captures_dir(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        path = create_capture_note(
            row_id=1, week="2026-W11", source_file="Timeline/note.md",
            thread_text="An interesting idea about local-first tools",
            suggested_action="Write a spec for a local-first tool registry",
            rationale="Fills a gap in the suite.",
            vault=vault, captures_dir="_captures", dry_run=False,
        )
        assert path.exists()
        assert path.parent == vault / "_captures"

    def test_note_contains_key_fields(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        path = create_capture_note(
            row_id=42, week="2026-W11", source_file="Timeline/note.md",
            thread_text="My original thought here",
            suggested_action="Build a thing",
            rationale="Because it matters.",
            vault=vault, captures_dir="_captures", dry_run=False,
        )
        content = path.read_text()
        assert "triage_id: 42" in content
        assert "My original thought here" in content
        assert "Because it matters." in content

    def test_dry_run_does_not_create_file(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        path = create_capture_note(
            row_id=1, week="2026-W11", source_file="note.md",
            thread_text="A thought", suggested_action="Do a thing", rationale="Why.",
            vault=vault, captures_dir="_captures", dry_run=True,
        )
        assert not path.exists()
        assert not (vault / "_captures").exists()


class TestAppendTask:
    def test_creates_daily_note_with_task(self, tmp_path):
        vault = tmp_path / "vault"
        (vault / "Timeline").mkdir(parents=True)
        path = append_task(
            suggested_action="Fix the scanner bug",
            source_file="Timeline/note.md",
            vault=vault, dry_run=False,
        )
        assert path.exists()
        content = path.read_text()
        assert "Fix the scanner bug" in content
        assert "- [ ]" in content
        assert "## Actions" in content

    def test_appends_to_existing_actions_section(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()
        note = vault / "Timeline" / f"{today.isoformat()}.md"
        note.parent.mkdir(parents=True)
        note.write_text(f"# {today.isoformat()}\n\n## Actions\n\n- [ ] Existing task\n")
        path = append_task("New task here", "note.md", vault, dry_run=False)
        content = path.read_text()
        assert "Existing task" in content
        assert "New task here" in content

    def test_dry_run_does_not_write(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        path = append_task(
            suggested_action="Fix the scanner bug",
            source_file="Timeline/note.md",
            vault=vault, dry_run=True,
        )
        assert not path.exists()


class TestRunAct:
    def _insert_row(self, conn, thread_text, human_disposition, suggested_action="Do the thing.", rationale="Because."):
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_action, rationale, human_disposition)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "Timeline/note.md", thread_text, "thought", suggested_action, rationale, human_disposition),
        )

    def test_creates_capture_note(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_row(conn, "An idea about local-first tools", "capture",
                         suggested_action="Write a spec for a local-first tool registry")
        conn.commit()
        conn.close()

        acted, deferred, errors = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert acted == 1
        assert errors == 0
        assert any((vault / "_captures").iterdir())

    def test_appends_task(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        (vault / "Timeline").mkdir(parents=True)
        conn = sqlite3.connect(db)
        self._insert_row(conn, "Fix the classifier noise issue", "task",
                         suggested_action="Add a noise filter to the classifier pipeline")
        conn.commit()
        conn.close()

        run_act(db, vault, "_captures", dry_run=False, verbose=False)
        today = date.today()
        daily_note = vault / "Timeline" / f"{today.isoformat()}.md"
        assert daily_note.exists()
        assert "noise filter" in daily_note.read_text()

    def test_stamps_executed_at_for_close_and_discard(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_row(conn, "Something done already", "close")
        self._insert_row(conn, "Just noise from the morning", "discard")
        conn.commit()
        conn.close()

        acted, _, _ = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert acted == 2
        conn = sqlite3.connect(db)
        stamped = conn.execute("SELECT COUNT(*) FROM thread_triage WHERE executed_at IS NOT NULL").fetchone()[0]
        conn.close()
        assert stamped == 2

    def test_skips_defers(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_row(conn, "Worth revisiting later", "defer")
        conn.commit()
        conn.close()

        acted, deferred, errors = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert acted == 0
        assert deferred == 1
        conn = sqlite3.connect(db)
        unstamped = conn.execute("SELECT COUNT(*) FROM thread_triage WHERE executed_at IS NULL").fetchone()[0]
        conn.close()
        assert unstamped == 1  # defer left untouched

    def test_dry_run_creates_no_files_and_stamps_nothing(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_row(conn, "A capture idea worth noting", "capture")
        conn.commit()
        conn.close()

        run_act(db, vault, "_captures", dry_run=True, verbose=False)
        assert not (vault / "_captures").exists()
        conn = sqlite3.connect(db)
        unstamped = conn.execute("SELECT COUNT(*) FROM thread_triage WHERE executed_at IS NULL").fetchone()[0]
        conn.close()
        assert unstamped == 1

    def test_defer_sets_resurface_after(self, tmp_path):
        """Deferred rows get resurface_after set so they can be tracked."""
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_row(conn, "Worth revisiting later this month", "defer")
        conn.commit()
        conn.close()

        acted, deferred, errors = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert deferred == 1
        assert acted == 0
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT resurface_after FROM thread_triage").fetchone()
        conn.close()
        assert row[0] is not None  # resurface_after was stamped
