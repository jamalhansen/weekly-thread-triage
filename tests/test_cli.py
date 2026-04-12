"""Integration tests for CLI commands: act, scan, classify, review, add."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from local_first_common.testing import MockProvider

from triage.logic import app

runner = CliRunner()

FIXTURE_NOTE = Path(__file__).parent / "fixtures" / "sample_daily_note.md"


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


def _insert_reviewed(conn, thread_text, human_disposition, resurface_after=None, search_term=None):
    """Insert a row with human_disposition already set (simulates post-Phase-3 state)."""
    conn.execute(
        """INSERT INTO thread_triage
           (week, source_file, thread_text, thread_type, human_disposition, resurface_after, search_term)
           VALUES (?,?,?,?,?,?,?)""",
        ("2026-W11", "a.md", thread_text, "thought", human_disposition, resurface_after, search_term),
    )


class TestActCommand:
    def test_act_writes_weekly_captures(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        (vault / "Timeline").mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_action, rationale, suggested_disposition)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "Timeline/2026-04-01.md", "Great idea about local-first agent design", "thought",
             "Write a spec for a local-first agent orchestrator", "Key gap in the suite.", "surface"),
        )
        conn.commit()
        conn.close()

        from datetime import date
        today = date.today()
        with patch("triage.config.VAULT_PATH", vault):
            result = runner.invoke(app, [
                "act", "--db", str(db), "--vault", str(vault), "--verbose",
            ])

        assert result.exit_code == 0, result.output
        assert "Weekly Captures" in result.output or "weekly captures" in result.output
        note = vault / "Timeline" / f"{today.isoformat()}.md"
        assert note.exists()
        assert "## Weekly Captures" in note.read_text()

    def test_act_dry_run(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        (vault / "Timeline").mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_action, rationale, suggested_disposition)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "Timeline/2026-04-01.md", "Another idea", "thought",
             "Write a spec", "Good reason.", "surface"),
        )
        conn.commit()
        conn.close()

        with patch("triage.config.VAULT_PATH", vault):
            result = runner.invoke(app, ["act", "--db", str(db), "--vault", str(vault), "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output


class TestScanCommand:
    def test_dry_run_shows_threads(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir()
        note.write_text(FIXTURE_NOTE.read_text())
        db = make_db(tmp_path)

        with patch("triage.config.VAULT_PATH", vault):
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

        with patch("triage.config.VAULT_PATH", vault):
            result = runner.invoke(app, [
                "scan", "--week", "2026-W11",
                "--db", str(db),
            ])

        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM thread_triage").fetchone()[0]
        conn.close()
        assert count > 0

    def test_missing_db_is_auto_created(self, tmp_path):
        db_path = tmp_path / "new-auto.db"
        result = runner.invoke(app, [
            "scan", "--db", str(db_path),
        ])
        assert result.exit_code == 0
        assert db_path.exists()


class TestClassifyCommand:
    def test_classifies_pending_rows_in_batch(self, tmp_path):
        """Classify makes one LLM call and marks selected rows as 'surface'."""
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text, thread_type) VALUES (?,?,?,?)",
            ("2026-W11", "Timeline/2026-04-01.md", "I should look into Apple Silicon hardware acceleration", "thought"),
        )
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text, thread_type) VALUES (?,?,?,?)",
            ("2026-W11", "Timeline/2026-04-02.md", "Just noise captured in the moment today", "thought"),
        )
        conn.commit()
        rows = conn.execute("SELECT id FROM thread_triage ORDER BY id").fetchall()
        conn.close()
        first_id, _ = rows[0][0], rows[1][0]

        mock_response = json.dumps({
            "items": [
                {
                    "id": first_id,
                    "suggested_action": "Check whether Pal uses Apple Silicon neural engine.",
                    "rationale": "Has concrete action potential and was likely forgotten.",
                }
            ]
        })

        with patch("triage.logic.resolve_provider", return_value=MockProvider(mock_response)):
            result = runner.invoke(app, ["classify", "--db", str(db)])

        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(db)
        rows = conn.execute("SELECT id, suggested_disposition FROM thread_triage ORDER BY id").fetchall()
        conn.close()
        assert rows[0][1] == "surface"
        assert rows[1][1] == "discard"

    def test_dry_run_does_not_write(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text) VALUES (?,?,?)",
            ("2026-W11", "Timeline/2026-04-01.md", "An interesting idea about personas in the tool suite"),
        )
        conn.commit()
        row_id = conn.execute("SELECT id FROM thread_triage").fetchone()[0]
        conn.close()

        mock_response = json.dumps({
            "items": [{"id": row_id, "suggested_action": "Build it.", "rationale": "Good idea."}]
        })

        with patch("triage.logic.resolve_provider", return_value=MockProvider(mock_response)):
            result = runner.invoke(app, ["classify", "--db", str(db), "--dry-run"])

        assert result.exit_code == 0
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT suggested_disposition FROM thread_triage").fetchone()
        conn.close()
        assert row[0] is None  # not written

    def test_context_file_is_prepended_to_system_prompt(self, tmp_path):
        """When a context file exists, its contents reach the LLM via the system prompt."""
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text, thread_type) VALUES (?,?,?,?)",
            ("2026-W11", "Timeline/2026-04-01.md", "Build a read-later queue for kept articles", "thought"),
        )
        conn.commit()
        row_id = conn.execute("SELECT id FROM thread_triage").fetchone()[0]
        conn.close()

        ctx_file = tmp_path / "context.md"
        ctx_file.write_text("## Tool Suite\n\n- content-discovery-agent\n- weekly-thread-triage\n")

        captured_prompts: list[str] = []

        class CapturingProvider:
            def complete(self, system: str, user: str) -> str:
                captured_prompts.append(system)
                return json.dumps({
                    "items": [{"id": row_id, "suggested_action": "Write a spec.", "rationale": "Good idea."}]
                })

        with patch("triage.logic.resolve_provider", return_value=CapturingProvider()):
            result = runner.invoke(app, [
                "classify", "--db", str(db),
                "--context-file", str(ctx_file),
            ])

        assert result.exit_code == 0, result.output
        assert len(captured_prompts) == 1
        assert "content-discovery-agent" in captured_prompts[0]
        assert "Personal Context" in captured_prompts[0]


class TestReviewCommand:
    def test_shows_surfaced_rows(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_disposition, suggested_action, rationale)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "Timeline/2026-04-01.md", "An idea worth reviewing", "thought",
             "surface", "Do the thing", "Good reason"),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["review", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "1 item" in result.output
        assert "An idea worth reviewing" in result.output or "Do the thing" in result.output

    def test_shows_past_due_defers(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        _insert_reviewed(conn, "Should have resurfaced by now", "defer", resurface_after="2020-01-01")
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["review", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "Past-due defers" in result.output
        assert "Should have resurfaced" in result.output

    def test_shows_nothing_when_empty(self, tmp_path):
        db = make_db(tmp_path)
        result = runner.invoke(app, ["review", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "Nothing to surface" in result.output


class TestAddCommand:
    def test_adds_row_to_db(self, tmp_path):
        db = make_db(tmp_path)
        result = runner.invoke(app, [
            "add", "A brand new idea captured during review",
            "--db", str(db), "--week", "2026-W11",
        ])
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT thread_text, thread_type, source_file FROM thread_triage").fetchone()
        conn.close()
        assert row[0] == "A brand new idea captured during review"
        assert row[1] == "thought"
        assert row[2] == "manual"

    def test_dry_run_does_not_write(self, tmp_path):
        db = make_db(tmp_path)
        result = runner.invoke(app, [
            "add", "This should not be saved",
            "--db", str(db), "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output
        conn = sqlite3.connect(db)
        count = conn.execute("SELECT COUNT(*) FROM thread_triage").fetchone()[0]
        conn.close()
        assert count == 0

    def test_respects_type_flag(self, tmp_path):
        db = make_db(tmp_path)
        runner.invoke(app, [
            "add", "Do the thing now",
            "--db", str(db), "--type", "task", "--week", "2026-W11",
        ])
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT thread_type FROM thread_triage").fetchone()
        conn.close()
        assert row[0] == "task"
