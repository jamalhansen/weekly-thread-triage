"""Tests for load_personal_context and load_goal_context."""

from datetime import date

from triage.logic import load_goal_context, load_personal_context


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
