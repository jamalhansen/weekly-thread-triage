"""Tests for thread extraction, file scanning, and deduplication."""

from datetime import date
from pathlib import Path
from unittest.mock import patch

from triage.logic import (
    ThreadRow,
    deduplicate,
    extract_threads,
    find_files_containing_dates,
)

FIXTURE_NOTE = Path(__file__).parent / "fixtures" / "sample_daily_note.md"


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
        """Files in .obsidian inside Timeline are not scanned."""
        vault = tmp_path
        hidden = vault / "Timeline" / ".obsidian" / "config.md"
        hidden.parent.mkdir(parents=True)
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
        """SKIP_PATHS filters files within scanned dirs."""
        vault = tmp_path
        # Put both files inside Timeline/ so they're in the scan root
        skipped = vault / "Timeline" / "_skip" / "note.md"
        skipped.parent.mkdir(parents=True)
        skipped.write_text("2026-03-12")
        kept = vault / "Timeline" / "2026-03-12.md"
        kept.parent.mkdir(parents=True, exist_ok=True)
        kept.write_text("2026-03-12")

        dates = [date(2026, 3, 12)]
        with patch("triage.scanner.SKIP_PATHS", {"_skip"}):
            result = find_files_containing_dates(vault, dates)
        assert skipped not in result
        assert kept in result

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

    def test_ignores_files_outside_scan_dirs(self, tmp_path):
        """Files outside Timeline/ (e.g. project notes) are never scanned."""
        vault = tmp_path
        project_note = vault / "projects" / "tools" / "_NOTES.md"
        project_note.parent.mkdir(parents=True)
        project_note.write_text("[[2026-03-12]] - Some changelog entry 2026-03-12")
        timeline_note = vault / "Timeline" / "2026-03-12.md"
        timeline_note.parent.mkdir(parents=True)
        timeline_note.write_text("Today is 2026-03-12")

        dates = [date(2026, 3, 12)]
        result = find_files_containing_dates(vault, dates)
        assert project_note not in result
        assert timeline_note in result

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

def test_high_signal_filtering():
    """Verify that short high-signal thoughts are kept and long noise is discarded."""
    from triage.scanner import extract_threads
    from pathlib import Path
    import tempfile
    
    content = """
## Thoughts
- Buy BTC
- I think that maybe I should do something
- ? Why is SQLite so fast
- Sometimes I feel like it is what it is
"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md') as tmp:
        tmp.write(content)
        tmp.flush()
        # Mock vault path as the temp dir
        threads = extract_threads(Path(tmp.name), Path(tmp.name).parent)
        
        texts = [t.thread_text for t in threads]
        assert "Buy BTC" in texts           # High-signal verb
        assert "? Why is SQLite so fast" in texts # Question
        assert "I think that maybe I should do something" not in texts # Noise starter
        assert "Sometimes I feel like it is what it is" not in texts   # Noise starter
