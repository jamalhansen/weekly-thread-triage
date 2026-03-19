"""Tests for weekly-thread-triage."""

import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from local_first_common.testing import MockProvider

from logic import (
    app,
    ThreadRow,
    _resolve_db_path,
    append_task,
    create_capture_note,
    dates_for_days,
    dates_for_week,
    deduplicate,
    extract_threads,
    find_files_containing_dates,
    load_goal_context,
    load_personal_context,
    run_act,
    slugify,
    week_label,
    write_rows,
)

# ── Helpers (continued) ───────────────────────────────────────────────────────

def _insert_reviewed(conn, thread_text, human_disposition, resurface_after=None):
    """Insert a row with human_disposition already set (simulates post-Phase-3 state)."""
    conn.execute(
        """INSERT INTO thread_triage
           (week, source_file, thread_text, thread_type, human_disposition, resurface_after)
           VALUES (?,?,?,?,?,?)""",
        ("2026-W11", "a.md", thread_text, "thought", human_disposition, resurface_after),
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

class TestResolveDbPath:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        explicit = tmp_path / "explicit.db"
        monkeypatch.setenv("LOCAL_FIRST_DB", str(explicit))
        assert _resolve_db_path() == explicit

    def test_discovers_sync_db_when_dir_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOCAL_FIRST_DB", raising=False)
        sync_dir = tmp_path / "thread-triage"
        sync_dir.mkdir()
        fake_sync_db = sync_dir / "thread-triage.db"
        with patch("logic._SYNC_DB", fake_sync_db):
            assert _resolve_db_path() == fake_sync_db

    def test_falls_back_to_legacy_when_sync_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOCAL_FIRST_DB", raising=False)
        fake_sync_db = tmp_path / "nonexistent" / "thread-triage.db"
        fake_legacy = tmp_path / "local-first.db"
        with patch("logic._SYNC_DB", fake_sync_db), \
             patch("logic._LEGACY_DB", fake_legacy):
            assert _resolve_db_path() == fake_legacy


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

    def test_skips_recurring_tasks_rotate_emoji(self, tmp_path):
        """Tasks containing the 🔄 emoji variant are also skipped."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "- [ ] Check email 🔄 every morning\n"
            "- [ ] Write the SQLite MCP spec doc\n"
        )
        threads = extract_threads(note, vault)
        tasks = [t for t in threads if t.thread_type == "task"]
        assert len(tasks) == 1
        assert "SQLite" in tasks[0].thread_text

    def test_skips_recurring_thoughts_rotate_emoji(self, tmp_path):
        """Thought bullets containing 🔄 are also skipped."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Thoughts\n\n"
            "- Remember to do the weekly review 🔄 every Sunday night\n"
            "- I keep thinking about the weekly review format and how it could be improved\n"
        )
        threads = extract_threads(note, vault)
        assert len(threads) == 1
        assert "weekly review format" in threads[0].thread_text

    def test_skips_task_fragments(self, tmp_path):
        """Tasks with fewer than 4 meaningful words after stripping metadata are skipped."""
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

    # ── Fix B: Unicode bullet characters ──────────────────────────────────────

    def test_extracts_unicode_bullet_dot(self, tmp_path):
        """∙ (U+2219) bullets in thought sections are extracted as thoughts."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Early Morning Chat Threads\n\n"
            "∙\tPersona-as-evaluator concept — assign personas to core life principles and evaluate monthly goals\n"
            "∙\t10-year positioning — become the person for one specific thing\n"
        )
        threads = extract_threads(note, vault)
        thoughts = [t for t in threads if t.thread_type == "thought"]
        assert len(thoughts) == 2
        assert any("Persona-as-evaluator" in t.thread_text for t in thoughts)
        assert any("10-year positioning" in t.thread_text for t in thoughts)

    def test_extracts_bullet_dot_variants(self, tmp_path):
        """• and · bullet variants are also extracted."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Thoughts\n\n"
            "• Using an MCP server as a unified backend could enable agentic orchestration\n"
            "· The local tools need a shared coordination layer to compose properly\n"
        )
        threads = extract_threads(note, vault)
        thoughts = [t for t in threads if t.thread_type == "thought"]
        assert len(thoughts) == 2

    def test_unicode_bullet_skips_short(self, tmp_path):
        """∙ bullets with fewer than 4 words are skipped."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text("## Thoughts\n\n∙\tYes\n∙\tFive meaningful words in this one\n")
        threads = extract_threads(note, vault)
        assert len(threads) == 1

    # ── Fix A: Obsidian callout block content ──────────────────────────────────

    def test_extracts_bullets_inside_morning_pages_callout(self, tmp_path):
        """Bullet lines inside a Morning Pages callout block are extracted."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## ✍️ Morning Pages\n\n"
            "> [!pencil]- Click to expand\n"
            ">\n"
            "> - Using an MCP server as a unified backend enables agentic orchestration\n"
            "> - The local tools need a shared coordination layer to compose them\n"
        )
        threads = extract_threads(note, vault)
        thoughts = [t for t in threads if t.thread_type == "thought"]
        assert len(thoughts) == 2
        assert any("MCP server" in t.thread_text for t in thoughts)

    def test_callout_opener_line_not_extracted(self, tmp_path):
        """The "> [!pencil]-" callout opener line itself is not extracted as a thread."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "> [!pencil]- Click to expand stream-of-consciousness writing\n"
            "> - One genuine insight about the MCP architecture pattern here\n"
        )
        threads = extract_threads(note, vault)
        assert all("expand" not in t.thread_text for t in threads)
        assert all("pencil" not in t.thread_text for t in threads)

    def test_callout_bullets_skipped_outside_thought_section(self, tmp_path):
        """Callout content in non-thought sections is not extracted."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Resources\n\n"
            "> [!note]- Reference\n"
            "> - Some technical detail that is not a thought\n"
        )
        threads = extract_threads(note, vault)
        assert len(threads) == 0

    def test_callout_unchecked_task_extracted(self, tmp_path):
        """An unchecked task inside a Morning Pages callout is extracted as a task."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "> [!pencil]- Expand\n"
            "> - [ ] Write the SQLite MCP spec document this week\n"
        )
        threads = extract_threads(note, vault)
        tasks = [t for t in threads if t.thread_type == "task"]
        assert len(tasks) == 1
        assert "SQLite MCP spec" in tasks[0].thread_text

    def test_callout_unicode_bullet_extracted(self, tmp_path):
        """∙ bullets inside a callout in a thought section are extracted."""
        vault = tmp_path
        note = vault / "note.md"
        note.write_text(
            "## Morning Pages\n\n"
            "> [!pencil]- Expand\n"
            "> ∙\tPersona-as-evaluator concept for monthly goal review sessions\n"
        )
        threads = extract_threads(note, vault)
        thoughts = [t for t in threads if t.thread_type == "thought"]
        assert len(thoughts) == 1
        assert "Persona-as-evaluator" in thoughts[0].thread_text


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
        with patch("logic.SKIP_PATHS", {"_marketing"}):
            result = find_files_containing_dates(vault, dates)
        assert marketing not in result
        assert timeline in result

    def test_skips_captures_dir(self, tmp_path):
        """Files in _captures are never scanned (triage output must not be re-scanned)."""
        vault = tmp_path
        capture = vault / "_captures" / "2026-03-12 some-capture.md"
        capture.parent.mkdir()
        capture.write_text("Some capture note referencing 2026-03-12.")

        dates = [date(2026, 3, 12)]
        result = find_files_containing_dates(vault, dates)
        assert capture not in result

    def test_does_not_match_date_only_in_frontmatter(self, tmp_path):
        """A file whose only date reference is in YAML frontmatter should not be matched.

        This prevents spec files (Created: 2026-03-12) from being scanned
        just because they were created this week.
        """
        vault = tmp_path
        spec = vault / "_series" / "tool-spec.md"
        spec.parent.mkdir()
        spec.write_text(
            "---\nCreated: 2026-03-12\nStatus: active\n---\n\n"
            "This spec was created this week but has no weekly date references in the body."
        )
        dates = [date(2026, 3, 12)]
        result = find_files_containing_dates(vault, dates)
        assert spec not in result

    def test_matches_date_in_body_despite_frontmatter(self, tmp_path):
        """A file with the date in BOTH frontmatter and body IS matched (body takes precedence)."""
        vault = tmp_path
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir()
        note.write_text(
            "---\nCreated: 2026-03-12\n---\n\n"
            "Today is 2026-03-12 and I worked on the project."
        )
        dates = [date(2026, 3, 12)]
        result = find_files_containing_dates(vault, dates)
        assert note in result


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


class TestSlugify:
    def test_basic_slug(self):
        assert slugify("Create a tool spec for YouTube") == "create-a-tool-spec-for-youtube"

    def test_strips_punctuation(self):
        assert "." not in slugify("Create a spec. With punctuation!")

    def test_respects_max_words(self):
        slug = slugify("one two three four five six seven eight nine ten", max_words=4)
        assert slug == "one-two-three-four"

    def test_handles_empty(self):
        assert slugify("") == ""


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


class TestLoadGoalContext:
    def test_returns_empty_when_no_goal_files(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        result = load_goal_context(vault, date(2026, 3, 12))
        assert result == ""

    def test_loads_yearly_goals(self, tmp_path):
        vault = tmp_path / "vault"
        yearly = vault / "Goals" / "2026" / "2026 Goals.md"
        yearly.parent.mkdir(parents=True)
        yearly.write_text("## Goals\n\n- Launch the tools suite\n- Write 12 blog posts\n")
        result = load_goal_context(vault, date(2026, 3, 12))
        assert "Launch the tools suite" in result
        assert "### 2026 Yearly Goals" in result

    def test_loads_monthly_focus(self, tmp_path):
        vault = tmp_path / "vault"
        monthly = vault / "Goals" / "2026" / "_monthly" / "2026-03.md"
        monthly.parent.mkdir(parents=True)
        monthly.write_text("## March Focus\n\n- Ship the weekly review tool\n")
        result = load_goal_context(vault, date(2026, 3, 12))
        assert "Ship the weekly review tool" in result
        assert "### 2026-03 Monthly Focus" in result

    def test_strips_frontmatter(self, tmp_path):
        vault = tmp_path / "vault"
        yearly = vault / "Goals" / "2026" / "2026 Goals.md"
        yearly.parent.mkdir(parents=True)
        yearly.write_text("---\ncreated: 2026-01-01\n---\n\n- Build more tools\n")
        result = load_goal_context(vault, date(2026, 3, 12))
        assert "created:" not in result
        assert "Build more tools" in result

    def test_strips_wikilinks(self, tmp_path):
        vault = tmp_path / "vault"
        yearly = vault / "Goals" / "2026" / "2026 Goals.md"
        yearly.parent.mkdir(parents=True)
        yearly.write_text("- Build [[weekly-thread-triage|the triage tool]]\n")
        result = load_goal_context(vault, date(2026, 3, 12))
        assert "[[" not in result
        assert "the triage tool" in result

    def test_includes_both_yearly_and_monthly(self, tmp_path):
        vault = tmp_path / "vault"
        yearly = vault / "Goals" / "2026" / "2026 Goals.md"
        yearly.parent.mkdir(parents=True)
        yearly.write_text("- Yearly goal one\n")
        monthly = vault / "Goals" / "2026" / "_monthly" / "2026-03.md"
        monthly.parent.mkdir(parents=True)
        monthly.write_text("- Monthly focus here\n")
        result = load_goal_context(vault, date(2026, 3, 12))
        assert "Yearly goal one" in result
        assert "Monthly focus here" in result


# ── Integration tests ─────────────────────────────────────────────────────────

class TestActCommand:
    def test_act_creates_captures_and_stamps(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_action, rationale, human_disposition)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "note.md", "Great idea about local-first agent design", "thought",
             "Write a spec for a local-first agent orchestrator", "Key gap in the suite.", "capture"),
        )
        conn.commit()
        conn.close()

        with patch("logic.VAULT_PATH", vault):
            result = runner.invoke(app, [
                "act", "--db", str(db),
                "--captures-dir", "_captures",
                "--verbose",
            ])

        assert result.exit_code == 0, result.output
        assert "capture" in result.output
        assert any((vault / "_captures").iterdir())

    def test_act_dry_run(self, tmp_path):
        db = make_db(tmp_path)
        vault = tmp_path / "vault"
        vault.mkdir()
        conn = sqlite3.connect(db)
        conn.execute(
            """INSERT INTO thread_triage
               (week, source_file, thread_text, thread_type, suggested_action, rationale, human_disposition)
               VALUES (?,?,?,?,?,?,?)""",
            ("2026-W11", "note.md", "Another idea", "thought",
             "Write a spec", "Good reason.", "capture"),
        )
        conn.commit()
        conn.close()

        with patch("logic.VAULT_PATH", vault):
            result = runner.invoke(app, ["act", "--db", str(db), "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output
        assert not (vault / "_captures").exists()


class TestScanCommand:
    def test_dry_run_shows_threads(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        note = vault / "Timeline" / "2026-03-12.md"
        note.parent.mkdir()
        note.write_text(FIXTURE_NOTE.read_text())
        db = make_db(tmp_path)

        with patch("logic.VAULT_PATH", vault):
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

        with patch("logic.VAULT_PATH", vault):
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

        with patch("logic.resolve_provider", return_value=MockProvider(mock_response)):
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

        with patch("logic.resolve_provider", return_value=MockProvider(mock_response)):
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

        with patch("logic.resolve_provider", return_value=CapturingProvider()):
            result = runner.invoke(app, [
                "classify", "--db", str(db),
                "--context-file", str(ctx_file),
            ])

        assert result.exit_code == 0, result.output
        assert len(captured_prompts) == 1
        assert "content-discovery-agent" in captured_prompts[0]
        assert "Personal Context" in captured_prompts[0]


class TestReviewCommand:
    def test_shows_pending_rows(self, tmp_path):
        db = make_db(tmp_path)
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO thread_triage (week, source_file, thread_text, thread_type, suggested_disposition) VALUES (?,?,?,?,?)",
            ("2026-W11", "a.md", "An idea worth reviewing", "thought", "capture"),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["review", "--db", str(db)])
        assert result.exit_code == 0, result.output
        assert "Pending review: 1 row" in result.output
        assert "An idea worth reviewing" in result.output

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
        assert "Nothing pending review" in result.output


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
