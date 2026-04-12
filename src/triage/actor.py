import re
import sqlite3
import typer
from datetime import date, timedelta
from pathlib import Path


def slugify(text: str, max_words: int = 8) -> str:
    """Convert text to a readable, filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    words = text.split()[:max_words]
    return "-".join(w for w in words if w)


def _extract_date_from_source(source_file: str) -> str | None:
    """Extract YYYY-MM-DD from a source path like Timeline/2026-04-01.md."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", source_file)
    return m.group(1) if m else None


def _render_template(template_text: str, note_date: date) -> str:
    """Substitute Obsidian Templater variables for a given date."""
    iso = note_date.isocalendar()
    week_str = f"{iso[0]}-W{iso[1]:02d}"
    yesterday = (note_date - timedelta(days=1)).isoformat()
    tomorrow = (note_date + timedelta(days=1)).isoformat()

    result = template_text
    result = result.replace("{{date:YYYY-MM-DD}}", note_date.isoformat())
    result = result.replace("{{date:YYYY-[W]W}}", week_str)
    result = result.replace("{{yesterday}}", yesterday)
    result = result.replace("{{tomorrow}}", tomorrow)
    return result


def write_weekly_captures(
    items: list[dict],
    vault: Path,
    note_date: date,
    dry_run: bool,
    template_path: Path | None = None,
) -> Path:
    """Append a ## Weekly Captures section to the daily note for note_date.

    Creates the note from template if it doesn't exist.
    Each item dict must have: thread_text, source_file, suggested_action (optional).
    """
    note_path = vault / "Timeline" / f"{note_date.isoformat()}.md"

    lines = ["## Weekly Captures", ""]
    for item in items:
        action = item.get("suggested_action") or item["thread_text"]
        source_date = _extract_date_from_source(item["source_file"])
        source_ref = f" *(from {source_date})*" if source_date else f" *(from {item['source_file']})*"
        lines.append(f"- [ ] {action}{source_ref}")
    lines.append("")
    section_text = "\n".join(lines)

    if dry_run:
        typer.echo(f"\n[dry-run] Would append to {note_path}:\n")
        typer.echo(section_text)
        return note_path

    # Create from template if the note doesn't exist
    if not note_path.exists():
        note_path.parent.mkdir(parents=True, exist_ok=True)
        if template_path and template_path.exists():
            raw = template_path.read_text(encoding="utf-8")
            content = _render_template(raw, note_date)
        else:
            content = f"# {note_date.isoformat()}\n\n## Thoughts\n\n## Actions\n\n"
        note_path.write_text(content, encoding="utf-8")

    existing = note_path.read_text(encoding="utf-8")
    note_path.write_text(existing.rstrip() + "\n\n" + section_text, encoding="utf-8")
    return note_path


def run_act(
    db: Path,
    vault: Path,
    captures_dir: str,
    dry_run: bool,
    verbose: bool,
    template_path: Path | None = None,
) -> tuple[int, int, int]:
    """Run Phase 4: write surfaced items to Weekly Captures in today's daily note."""
    conn = sqlite3.connect(db)
    acted = deferred = errors = 0
    try:
        # Primary path: items selected by batch classifier
        surface_rows = conn.execute(
            """SELECT id, week, source_file, thread_text, suggested_action, rationale
               FROM thread_triage
               WHERE suggested_disposition = 'surface' AND executed_at IS NULL
               ORDER BY created_at"""
        ).fetchall()

        if surface_rows:
            items = [
                {
                    "thread_text": r[3],
                    "source_file": r[2],
                    "suggested_action": r[4],
                }
                for r in surface_rows
            ]
            try:
                note_path = write_weekly_captures(
                    items, vault, date.today(), dry_run, template_path
                )
                typer.echo(f"  [weekly captures] {len(surface_rows)} item(s) → {note_path.name}")
                if not dry_run:
                    for r in surface_rows:
                        conn.execute(
                            "UPDATE thread_triage SET executed_at = datetime('now') WHERE id = ?",
                            (r[0],),
                        )
                acted += len(surface_rows)
            except Exception as e:
                typer.echo(f"  [error] writing weekly captures: {e}", err=True)
                errors += len(surface_rows)

        # Legacy path: rows where human_disposition was set manually (e.g. deferred items)
        legacy_rows = conn.execute(
            """SELECT id, week, source_file, thread_text, suggested_action, rationale, human_disposition
               FROM thread_triage
               WHERE human_disposition IS NOT NULL AND executed_at IS NULL
               ORDER BY created_at"""
        ).fetchall()

        for row_id, week, source_file, thread_text, suggested_action, rationale, disp in legacy_rows:
            try:
                if disp == "defer":
                    deferred += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE thread_triage SET resurface_after = date('now', '+7 days') WHERE id = ?",
                            (row_id,),
                        )
                    if verbose:
                        typer.echo(f"  [defer] resurface after 7 days: {thread_text[:60]}…")
                    continue

                elif disp in ("close", "discard"):
                    if verbose:
                        typer.echo(f"  [{disp}] {thread_text[:70]}…")

                if not dry_run:
                    conn.execute(
                        "UPDATE thread_triage SET executed_at = datetime('now') WHERE id = ?",
                        (row_id,),
                    )
                acted += 1

            except Exception as e:
                typer.echo(f"  [error] Row {row_id}: {e}", err=True)
                errors += 1

        conn.commit()
        return acted, deferred, errors
    finally:
        conn.close()
