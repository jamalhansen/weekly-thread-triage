"""Tests for write_weekly_captures and run_act."""

import sqlite3
from datetime import date
from pathlib import Path

from triage.actor import write_weekly_captures, run_act


SAMPLE_TEMPLATE = """\
---
day: "{{date:YYYY-MM-DD}}"
Previous: "[[{{yesterday}}]]"
Next: "[[{{tomorrow}}]]"
Week: "[[{{date:YYYY-[W]W}}]]"
tags:
  - daily
---

## Thoughts

## Actions
"""


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


class TestWriteWeeklyCaptures:
    def test_appends_section_to_existing_note(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()
        note = vault / "Timeline" / f"{today.isoformat()}.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Today\n\n## Thoughts\n\n## Actions\n")

        items = [
            {"thread_text": "I should look into Apple Silicon", "source_file": "Timeline/2026-04-01.md", "suggested_action": "Check whether Pal uses Apple Silicon hardware acceleration"},
        ]
        path = write_weekly_captures(items, vault, today, dry_run=False)

        content = path.read_text()
        assert "## Weekly Captures" in content
        assert "Apple Silicon" in content
        assert "*(from 2026-04-01)*" in content

    def test_creates_note_from_template_when_missing(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()
        template_path = vault / "Templates" / "Daily Note.md"
        template_path.parent.mkdir(parents=True)
        template_path.write_text(SAMPLE_TEMPLATE)

        items = [{"thread_text": "An idea", "source_file": "Timeline/2026-04-01.md", "suggested_action": "Do the thing"}]
        path = write_weekly_captures(items, vault, today, dry_run=False, template_path=template_path)

        assert path.exists()
        content = path.read_text()
        assert today.isoformat() in content          # template var substituted
        assert "## Weekly Captures" in content

    def test_creates_note_without_template_when_template_missing(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()

        items = [{"thread_text": "An idea", "source_file": "Timeline/2026-04-01.md", "suggested_action": "Do the thing"}]
        path = write_weekly_captures(items, vault, today, dry_run=False, template_path=None)

        assert path.exists()
        assert "## Weekly Captures" in path.read_text()

    def test_source_ref_uses_filename_date(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()
        note = vault / "Timeline" / f"{today.isoformat()}.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Today\n")

        items = [{"thread_text": "Thought", "source_file": "Timeline/2026-03-28.md", "suggested_action": "Act on it"}]
        path = write_weekly_captures(items, vault, today, dry_run=False)
        assert "*(from 2026-03-28)*" in path.read_text()

    def test_dry_run_does_not_write(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()
        items = [{"thread_text": "Idea", "source_file": "Timeline/2026-04-01.md", "suggested_action": "Do it"}]
        path = write_weekly_captures(items, vault, today, dry_run=True)
        assert not path.exists()

    def test_multiple_items_all_appear(self, tmp_path):
        vault = tmp_path / "vault"
        today = date.today()
        note = vault / "Timeline" / f"{today.isoformat()}.md"
        note.parent.mkdir(parents=True)
        note.write_text("# Today\n")

        items = [
            {"thread_text": "First idea", "source_file": "Timeline/2026-04-01.md", "suggested_action": "Do first"},
            {"thread_text": "Second idea", "source_file": "Timeline/2026-04-02.md", "suggested_action": "Do second"},
            {"thread_text": "Third idea", "source_file": "Timeline/2026-04-03.md", "suggested_action": "Do third"},
        ]
        path = write_weekly_captures(items, vault, today, dry_run=False)
        content = path.read_text()
        assert content.count("- [ ]") == 3


class TestRunAct:
    def _insert_surface(self, conn, thread_text, suggested_action="Do the thing.", rationale="Because."):
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_disposition, suggested_action, rationale)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "Timeline/2026-04-01.md", thread_text, "thought", "surface", suggested_action, rationale),
        )

    def _insert_legacy(self, conn, thread_text, human_disposition, resurface_after=None):
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, human_disposition, resurface_after)
               VALUES (?,?,?,?,?,?)""",
            ("2026-W11", "Timeline/note.md", thread_text, "thought", human_disposition, resurface_after),
        )

    def test_writes_weekly_captures_section(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        today = date.today()
        (vault / "Timeline").mkdir(parents=True)
        conn = sqlite3.connect(db)
        self._insert_surface(conn, "An interesting idea about local-first tools",
                             suggested_action="Write a spec for a local-first tool registry")
        conn.commit()
        conn.close()

        acted, deferred, errors = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert acted == 1
        assert errors == 0

        note = vault / "Timeline" / f"{today.isoformat()}.md"
        assert note.exists()
        assert "## Weekly Captures" in note.read_text()
        assert "local-first tool registry" in note.read_text()

    def test_stamps_executed_at_after_act(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        (vault / "Timeline").mkdir(parents=True)
        conn = sqlite3.connect(db)
        self._insert_surface(conn, "An idea to act on")
        conn.commit()
        conn.close()

        run_act(db, vault, "_captures", dry_run=False, verbose=False)
        conn = sqlite3.connect(db)
        stamped = conn.execute("SELECT COUNT(*) FROM thread_triage WHERE executed_at IS NOT NULL").fetchone()[0]
        conn.close()
        assert stamped == 1

    def test_legacy_defer_sets_resurface_after(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_legacy(conn, "Worth revisiting later", "defer")
        conn.commit()
        conn.close()

        acted, deferred, errors = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert deferred == 1
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT resurface_after FROM thread_triage").fetchone()
        conn.close()
        assert row[0] is not None

    def test_legacy_close_and_discard_stamp_executed_at(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        self._insert_legacy(conn, "Already done", "close")
        self._insert_legacy(conn, "Just noise", "discard")
        conn.commit()
        conn.close()

        acted, _, _ = run_act(db, vault, "_captures", dry_run=False, verbose=False)
        assert acted == 2
        conn = sqlite3.connect(db)
        stamped = conn.execute("SELECT COUNT(*) FROM thread_triage WHERE executed_at IS NOT NULL").fetchone()[0]
        conn.close()
        assert stamped == 2

    def test_dry_run_writes_nothing(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        (vault / "Timeline").mkdir(parents=True)
        conn = sqlite3.connect(db)
        self._insert_surface(conn, "An idea")
        conn.commit()
        conn.close()

        run_act(db, vault, "_captures", dry_run=True, verbose=False)
        conn = sqlite3.connect(db)
        unstamped = conn.execute("SELECT COUNT(*) FROM thread_triage WHERE executed_at IS NULL").fetchone()[0]
        conn.close()
        assert unstamped == 1
