import re
import sqlite3
import typer
from datetime import date
from pathlib import Path

def slugify(text: str, max_words: int = 8) -> str:
    """Convert text to a readable, filename-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)       # strip punctuation
    words = text.split()[:max_words]
    return "-".join(w for w in words if w)

def create_capture_note(
    row_id: int,
    week: str,
    source_file: str,
    thread_text: str,
    suggested_action: str,
    rationale: str,
    vault: Path,
    captures_dir: str,
    dry_run: bool,
) -> Path:
    """Write an Obsidian capture note. Returns the target path (even in dry-run)."""
    today = date.today().strftime("%Y-%m-%d")
    slug = slugify(suggested_action or thread_text)
    note_dir = vault / captures_dir
    note_path = note_dir / f"{today} {slug}.md"
    title = (suggested_action or thread_text).rstrip(".")

    content = (
        f"---\n"
        f"created: {today}\n"
        f"week: {week}\n"
        f"type: capture\n"
        f"source: {source_file}\n"
        f"triage_id: {row_id}\n"
        f"---\n\n"
        f"# {title}\n\n"
        f"> {thread_text}\n\n"
        f"## Rationale\n\n"
        f"{rationale}\n"
    )

    if not dry_run:
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path.write_text(content, encoding="utf-8")

    return note_path

def append_task(
    suggested_action: str,
    source_file: str,
    vault: Path,
    dry_run: bool,
) -> Path:
    """Append a task line to today's daily note Actions section."""
    today = date.today()
    note_path = vault / "Timeline" / f"{today.isoformat()}.md"
    task_line = f"- [ ] {suggested_action} ([[{source_file}]])"

    if not dry_run:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        content = note_path.read_text(encoding="utf-8") if note_path.exists() else f"# {today.isoformat()}\n\n## Actions\n\n"

        actions_heading = "\n## Actions\n"
        if actions_heading in content:
            before, _, after = content.partition(actions_heading)
            if "\n## " in after:
                idx = after.index("\n## ")
                new_content = before + actions_heading + after[:idx].rstrip() + f"\n{task_line}\n" + after[idx:]
            else:
                new_content = content.rstrip() + f"\n{task_line}\n"
        else:
            new_content = content.rstrip() + f"\n\n## Actions\n\n{task_line}\n"

        note_path.write_text(new_content, encoding="utf-8")

    return note_path

def run_act(
    db: Path,
    vault: Path,
    captures_dir: str,
    dry_run: bool,
    verbose: bool,
) -> tuple[int, int, int]:
    """Run Phase 4: act on all reviewed rows that haven't been executed yet."""
    conn = sqlite3.connect(db)
    acted = deferred = errors = 0
    try:
        pending = conn.execute(
            """SELECT id, week, source_file, thread_text,
                      suggested_action, rationale, human_disposition
               FROM thread_triage
               WHERE human_disposition IS NOT NULL AND executed_at IS NULL
               ORDER BY created_at"""
        ).fetchall()

        if not pending:
            typer.echo("No rows pending action.")
            return 0, 0, 0

        if verbose:
            typer.echo(f"[verbose] {len(pending)} rows to act on...")

        for row_id, week, source_file, thread_text, suggested_action, rationale, disp in pending:
            try:
                if disp == "capture":
                    path = create_capture_note(
                        row_id, week, source_file, thread_text,
                        suggested_action or "", rationale or "",
                        vault, captures_dir, dry_run,
                    )
                    typer.echo(f"  [capture] {path.name}")

                elif disp == "task":
                    path = append_task(
                        suggested_action or thread_text, source_file,
                        vault, dry_run,
                    )
                    typer.echo(f"  [task] \u2192 {path.name}")

                elif disp in ("close", "discard"):
                    if verbose:
                        typer.echo(f"  [{disp}] {thread_text[:70]}\u2026")

                elif disp == "defer":
                    deferred += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE thread_triage SET resurface_after = date('now', '+7 days') WHERE id = ?",
                            (row_id,),
                        )
                    if verbose:
                        typer.echo(f"  [defer] resurface after 7 days: {thread_text[:60]}\u2026")
                    continue

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
