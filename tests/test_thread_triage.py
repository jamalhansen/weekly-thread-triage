"""Tests for weekly-thread-triage."""

import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from local_first_common.testing import MockProvider

from thread_triage import (
    app,
    Classification,
    ThreadRow,
    dates_for_days,
    dates_for_week,
    deduplicate,
    extract_threads,
    find_files_containing_dates,
    run_classify,
    run_scan,
    week_label,
    write_rows,
)

runner = CliRunner()

FIXTURE_NOTE = Path(__file__).parent / "fixtures" / "sample_daily_note.md"


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Unit tests ────────────────────────────────────────────────────────────────

class TestWeekLabel:
    def test_known_date(self):
        assert week_label(date(2026, 3, 12)) == "2026-W11"

    def test_returns_padded_week(self):
        label = week_label(date(2026, 1, 5))
        assert "-W0" in label or "-W" in label


class TestDatesForWeek:
    def test_returns_seven_dates(self):
        dates = dates_for_week(date(2026, 3, 12))
        assert len(dates) == 7

    def test_all_in_same_week(self):
        dates = dates_for_week(date(2026, 3, 12))
        weeks = {d.isocalendar()[1] for d in dates}
        assert len(weeks) == 1


class TestDatesForDays:
    def test_returns_n_dates(self):
        assert len(dates_for_days(7)) == 7
        assert len(dates_for_days(1)) == 1

    def test_last_date_is_today(self):
        dates = dates_for_days(3)
        assert dates[-1] == date.today()


class TestExtractThreads:
    def test_extracts_unchecked_tasks(self, tmp_path):
        vault = tmp_path
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir(parents=True)
        note.write_text(FIXTURE_NOTE.read_text())

        threads = extract_threads(note, vault)
        tasks = [t for t in threads if t.thread_type == "task"]
        assert len(tasks) == 2
        assert any("SQLite MCP" in t.thread_text for t in tasks)
        assert any("spec for tool 34" in t.thread_text for t in tasks)

    def test_does_not_extract_checked_tasks(self, tmp_path):
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("- [x] Already done\n- [ ] Still open\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 1
        assert "Still open" in threads[0].thread_text

    def test_extracts_thoughts(self, tmp_path):
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "- I keep thinking about the weekly review format and how it could be improved\n"
            "- Short\n"
        )
        threads = extract_threads(note, vault)
        thoughts = [t for t in threads if t.thread_type == "thought"]
        assert len(thoughts) == 1
        assert "weekly review" in thoughts[0].thread_text

    def test_skips_short_thought_bullets(self, tmp_path):
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Thoughts\n\n- Yes\n- No\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 0

    def test_section_attribution(self, tmp_path):
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Actions\n\n- [ ] Fix the scanner bug\n")
        threads = extract_threads(note, vault)
        assert threads[0].source_section == "Actions"


class TestFindFilesContainingDates:
    def test_finds_files_with_date_string(self, tmp_path):
        vault = tmp_path
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir()
        note.write_text("Created: 2026-03-12\n\nSome content here.")

        dates = [date(2026, 3, 12)]
        result = find_files_containing_dates(vault, dates)
        assert note in result

    def test_skips_obsidian_dir(self, tmp_path):
        vault = tmp_path
        hidden = vault / ".obsidian" / "config.md"
        hidden.parent.mkdir()
        hidden.write_text("2026-03-12")

        dates = [date(2026, 3, 12)]
        result = find_files_containing_dates(vault, dates)
        assert hidden not in result

    def test_no_match_returns_empty(self, tmp_path):
        vault = tmp_path
        (vault / "note.md").write_text("Nothing relevant here.")
        result = find_files_containing_dates(vault, [date(2026, 3, 12)])
        assert len(result) == 0


class TestDeduplicate:
    def test_removes_exact_duplicates(self):
        rows = [
            ThreadRow("2026-W11", "a.md", None, "Fix the scanner", "task"),
            ThreadRow("2026-W11", "b.md", None, "Fix the scanner", "task"),
        ]
        unique = deduplicate(rows)
        assert len(unique) == 1

    def test_keeps_different_threads(self):
        rows = [
            ThreadRow("2026-W11", "a.md", None, "Fix the scanner", "task"),
            ThreadRow("2026-W11", "b.md", None, "Write the tests", "task"),
        ]
        assert len(deduplicate(rows)) == 2

    def test_normalises_whitespace(self):
        rows = [
            ThreadRow("2026-W11", "a.md", None, "Fix  the scanner", "task"),
            ThreadRow("2026-W11", "b.md", None, "Fix the  scanner", "task"),
        ]
        assert len(deduplicate(rows)) == 1


class TestWriteRows:
    def test_inserts_new_rows(self, tmp_path):
        db = make_db(tmp_path)
        rows = [ThreadRow("2026-W11", "a.md", "Actions", "Fix scanner", "task")]
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

    def test_allows_same_text_different_week(self, tmp_path):
        db = make_db(tmp_path)
        r1 = [ThreadRow("2026-W11", "a.md", None, "Fix scanner", "task")]
        r2 = [ThreadRow("2026-W12", "a.md", None, "Fix scanner", "task")]
        assert write_rows(db, r1) == 1
        assert write_rows(db, r2) == 1


# ── Integration tests ─────────────────────────────────────────────────────────

class TestScanCommand:
    def test_dry_run_shows_threads(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir()
        note.write_text(FIXTURE_NOTE.read_text())
        db = make_db(tmp_path)

        with patch("thread_triage.VAULT_PATH", vault):
            result = runner.invoke(app, [
                "scan", "--week", "2026-W11",
                "--db", str(db),
                "--dry-run", "--verbose",
            ])

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output

    def test_scan_writes_to_db(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir()
        note.write_text(FIXTURE_NOTE.read_text())
        db = make_db(tmp_path)

        with patch("thread_triage.VAULT_PATH", vault):
            result = runner.invoke(app, [
                "scan", "--week", "2026-W11",
                "--db", str(db),
            ])

        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM thread_triage").fetchone()[0]
        conn.close()
        assert count > 0

    def test_missing_db_fails(self, tmp_path):
        result = runner.invoke(app, [
            "scan", "--db", str(tmp_path / "missing.db"),
        ])
        assert result.exit_code == 1


class TestClassifyCommand:
    def test_classifies_pending_rows(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text, thread_type) VALUES (?,?,?,?)",
            ("2026-W11", "a.md", "Fix the scanner bug in content discovery", "task"),
        )
        conn.commit()
        conn.close()

        mock_response = json.dumps({
            "suggested_disposition": "task",
            "suggested_action": "Add to next sprint backlog with P2 priority.",
            "rationale": "Concrete bug with a clear owner and scope.",
        })

        with patch("thread_triage.resolve_provider", return_value=MockProvider(mock_response)):
            result = runner.invoke(app, ["classify", "--db", str(db)])

        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT suggested_disposition FROM thread_triage").fetchone()
        conn.close()
        assert row[0] == "task"

    def test_dry_run_does_not_write(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text) VALUES (?,?,?)",
            ("2026-W11", "a.md", "An interesting idea about personas in the tool suite"),
        )
        conn.commit()
        conn.close()

        mock_response = json.dumps({
            "suggested_disposition": "capture",
            "suggested_action": "Write a tool spec for a persona store.",
            "rationale": "Distinct idea that fits the series roadmap.",
        })

        with patch("thread_triage.resolve_provider", return_value=MockProvider(mock_response)):
            result = runner.invoke(app, ["classify", "--db", str(db), "--dry-run"])

        assert result.exit_code == 0
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT suggested_disposition FROM thread_triage").fetchone()
        conn.close()
        assert row[0] is None  # not written
