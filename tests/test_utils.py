"""Tests for utility functions: path resolution, date helpers, slugify."""

from datetime import date
from unittest.mock import patch

from triage.config import _resolve_db_path
from triage.actor import slugify
from triage.logic import (
    dates_for_days,
    dates_for_week,
    week_label,
)


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
        with patch("triage.config._SYNC_DB", fake_sync_db):
            assert _resolve_db_path() == fake_sync_db

    def test_falls_back_to_legacy_when_sync_absent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOCAL_FIRST_DB", raising=False)
        fake_sync_db = tmp_path / "nonexistent" / "thread-triage.db"
        fake_legacy = tmp_path / "local-first.db"
        with patch("triage.config._SYNC_DB", fake_sync_db), \
             patch("triage.config._LEGACY_DB", fake_legacy):
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
        assert len(dates_for_days(date.today(), 7)) == 7
        assert len(dates_for_days(date.today(), 1)) == 1

    def test_last_date_is_today(self):
        dates = dates_for_days(date.today(), 3)
        assert dates[-1] == date.today()


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
