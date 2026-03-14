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
    load_personal_context,
    run_classify,
    run_scan,
    week_label,
    write_rows,
    CONTEXT_FILE,
    SKIP_PATHS,
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
        """Tasks in thought sections (Morning Pages) are extracted."""
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
        """[x] completed tasks are skipped even inside thought sections."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Morning Pages\n\n- [x] Already done\n- [ ] Still open task here\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 1
        assert "Still open" in threads[0].thread_text

    def test_does_not_extract_cancelled_tasks(self, tmp_path):
        """[-] cancelled tasks are skipped even inside thought sections."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Morning Pages\n\n- [-] Decided not to do this\n- [ ] Still want to do this one\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 1
        assert "Still want" in threads[0].thread_text

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

    def test_skips_sql_keywords_in_thoughts(self, tmp_path):
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Thoughts\n\n"
            "- select * from customers\n"
            "- Select count(*) from orders where status is null\n"
            "- I keep thinking about how to improve the weekly review flow\n"
        )
        threads = extract_threads(note, vault)
        texts = [t.thread_text for t in threads]
        assert not any("select" in t.lower() for t in texts)
        assert any("weekly review" in t for t in texts)

    def test_skips_tasks_in_non_thought_sections(self, tmp_path):
        """Tasks under structured sections like Actions are not extracted.
        Those are managed by the Obsidian Tasks plugin."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Actions\n\n- [ ] Fix the scanner bug\n- [ ] Write the report\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 0

    def test_skips_recurring_tasks(self, tmp_path):
        """Tasks containing the 🔁 emoji are skipped — already tracked by Tasks plugin."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "- [ ] Walk the dog 🔁 every day\n"
            "- [ ] Set up SQLite MCP server\n"
        )
        threads = extract_threads(note, vault)
        tasks = [t for t in threads if t.thread_type == "task"]
        assert len(tasks) == 1
        assert "SQLite" in tasks[0].thread_text

    def test_skips_task_fragments(self, tmp_path):
        """Tasks with fewer than 3 meaningful words after stripping metadata are skipped."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "- [ ] 📅 2026-03-14\n"              # just a date — no meaningful words
            "- [ ] TBD\n"                          # one word
            "- [ ] Write the SQLite MCP spec\n"   # 5 meaningful words — keep
        )
        threads = extract_threads(note, vault)
        tasks = [t for t in threads if t.thread_type == "task"]
        assert len(tasks) == 1
        assert "SQLite MCP spec" in tasks[0].thread_text

    def test_section_attribution(self, tmp_path):
        """Source section is recorded correctly for extracted tasks."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Morning Pages\n\n- [ ] Fix the scanner bug here\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 1
        assert threads[0].source_section == "Morning Pages"


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

    def test_skips_paths_matching_skip_paths(self, tmp_path):
        vault = tmp_path
        marketing = vault / "_marketing" / "Starter Pack.md"
        marketing.parent.mkdir()
        marketing.write_text("2026-03-12")
        timeline = vault / "Timeline" / "2026-03-12.md"
        timeline.parent.mkdir()
        timeline.write_text("2026-03-12")

        dates = [date(2026, 3, 12)]
        with patch("thread_triage.SKIP_PATHS", {"_marketing"}):
            result = find_files_containing_dates(vault, dates)
        assert marketing not in result
        assert timeline in result


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


class TestLoadPersonalContext:
    def test_returns_empty_string_when_file_missing(self, tmp_path):
        missing = tmp_path / "no-such-file.md"
        assert load_personal_context(missing) == ""

    def test_returns_file_content_when_present(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text("## Tool Suite\n\n- content-discovery-agent\n- transcription-summarizer\n")
        result = load_personal_context(ctx)
        assert "content-discovery-agent" in result
        assert "transcription-summarizer" in result

    def test_strips_leading_trailing_whitespace(self, tmp_path):
        ctx = tmp_path / "context.md"
        ctx.write_text("\n\n## Context\n\n- Some tool\n\n\n")
        result = load_personal_context(ctx)
        assert result == result.strip()


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

    def test_context_file_is_prepended_to_prompt(self, tmp_path):
        """When a context file exists, its contents reach the LLM via the system prompt."""
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text, thread_type) VALUES (?,?,?,?)",
            ("2026-W11", "a.md", "Build a read-later queue for kept articles", "thought"),
        )
        conn.commit()
        conn.close()

        ctx_file = tmp_path / "context.md"
        ctx_file.write_text("## Tool Suite\n\n- content-discovery-agent\n- weekly-thread-triage\n")

        captured_prompts: list[str] = []

        class CapturingProvider:
            def complete(self, system: str, user: str) -> str:
                captured_prompts.append(system)
                return json.dumps({
                    "suggested_disposition": "capture",
                    "suggested_action": "Write a spec.",
                    "rationale": "Good idea.",
                })

        with patch("thread_triage.resolve_provider", return_value=CapturingProvider()):
            result = runner.invoke(app, [
                "classify", "--db", str(db),
                "--context-file", str(ctx_file),
            ])

        assert result.exit_code == 0, result.output
        assert len(captured_prompts) == 1
        assert "content-discovery-agent" in captured_prompts[0]
        assert "Personal Context" in captured_prompts[0]
