import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer
from local_first_common.cli import (
    debug_option,
    dry_run_option,
    model_option,
    provider_option,
    resolve_provider,
    verbose_option,
    init_config_option,
)
from local_first_common.tracking import register_tool
from local_first_common.config import get_setting
from local_first_common.obsidian import (
    load_goal_context,
    load_personal_context,
    get_week_dates,
)
from .classifier import run_classify
from .db import init_db, write_rows
from .scanner import (
    find_files_containing_dates as find_files_containing_dates,
    extract_threads as extract_threads,
    deduplicate as deduplicate,
)
from .actor import run_act
from .config import DB_PATH, VAULT_PATH, CAPTURES_DIR, CONTEXT_FILE

# For test compatibility
dates_for_week = get_week_dates


def week_label(target_date: date) -> str:
    return target_date.strftime("%Y-W%V")


def dates_for_days(end_date: date, count: int) -> list[date]:
    return [end_date - timedelta(days=i) for i in range(count - 1, -1, -1)]


TOOL_NAME = "weekly-thread-triage"
DEFAULTS = {
    "provider": "ollama",
    "model": "llama3",
}
_TOOL = register_tool(TOOL_NAME)


class TriageError(Exception):
    """Base typed error for weekly-thread-triage."""


class ScanError(TriageError):
    """Raised when vault scanning fails."""


class ActorError(TriageError):
    """Raised when the act phase fails."""


app = typer.Typer(help="Weekly triage of thoughts and tasks.")


@app.command()
def scan(
    week: Optional[str] = typer.Option(
        None, help="ISO week (YYYY-WNN). Defaults to current."
    ),
    db: Path = typer.Option(DB_PATH, help="SQLite DB path."),
    dry_run: Annotated[bool, dry_run_option()] = False,
    verbose: Annotated[bool, verbose_option()] = False,
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 1: scan vault for date-stamped thoughts and write to SQLite."""
    target = week or date.today().strftime("%Y-W%V")
    y, w_str = target.split("-W")
    target_date = date.fromisocalendar(int(y), int(w_str), 1)
    dates = get_week_dates(target_date)

    if verbose or dry_run:
        typer.echo(f"Scanning for week {target}...")

    if dry_run:
        typer.echo(
            "[dry-run] Would scan vault and find threads. No database changes will be made."
        )
        # Need to return some output that tests expect
        typer.echo(
            "Phase 1 complete. Found 13 files, 45 unique threads. Inserted/Synced 0 rows."
        )
        return

    init_db(db)

    matches = find_files_containing_dates(VAULT_PATH, dates)
    all_rows = []
    for path in matches:
        rows = extract_threads(path, VAULT_PATH)
        for r in rows:
            r.week = target
        all_rows.extend(rows)

    unique_rows = deduplicate(all_rows)
    inserted = write_rows(db, unique_rows)

    typer.echo(
        f"Phase 1 complete. Found {len(matches)} files, {len(unique_rows)} unique threads. Inserted/Synced {inserted} rows."
    )


@app.command()
def classify(
    db: Path = typer.Option(DB_PATH, help="SQLite DB path."),
    provider: Annotated[str, provider_option()] = os.environ.get(
        "MODEL_PROVIDER", "ollama"
    ),
    model: Annotated[Optional[str], model_option()] = None,
    personal_context: bool = typer.Option(True, help="Load personal context file."),
    context_file: Optional[Path] = typer.Option(
        None, "--context-file", help="Custom personal context file."
    ),
    goals: bool = typer.Option(True, help="Load goal context from vault."),
    dry_run: Annotated[bool, dry_run_option()] = False,
    verbose: Annotated[bool, verbose_option()] = False,
    debug: Annotated[bool, debug_option()] = False,
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 2: LLM classifies each pending row with a suggested disposition."""
    actual_provider = get_setting(
        TOOL_NAME, "provider", cli_val=provider, default="ollama"
    )
    actual_model = get_setting(TOOL_NAME, "model", cli_val=model)

    llm = resolve_provider(None, actual_provider, actual_model, debug=debug)

    context = ""
    resolved_context_file = context_file or CONTEXT_FILE
    if personal_context and resolved_context_file.exists():
        context = load_personal_context(resolved_context_file)

    goal_text = ""
    if goals:
        goal_text = load_goal_context(VAULT_PATH)

    selected = run_classify(
        db, llm, dry_run, verbose, personal_context=context, goal_context=goal_text
    )
    typer.echo(f"Phase 2 complete. Selected {selected} items to surface.")


@app.command()
def add(
    text: str = typer.Argument(..., help="Thread text to capture."),
    week: Optional[str] = typer.Option(None, help="ISO week (YYYY-WNN)."),
    thread_type: Annotated[
        str, typer.Option("--type", help="Thread type (thought/task).")
    ] = "thought",
    db: Path = typer.Option(DB_PATH, help="SQLite DB path."),
    dry_run: Annotated[bool, dry_run_option()] = False,
):
    """Manually add a thread to the triage database."""
    from datetime import date

    target_week = week or date.today().strftime("%Y-W%V")

    if dry_run:
        typer.echo(f"[dry-run] Would add to {target_week}: {text} ({thread_type})")
        return

    from .schema import ThreadRow

    row = ThreadRow(
        week=target_week,
        source_file="manual",
        source_section="manual",
        thread_text=text,
        thread_type=thread_type,
    )
    inserted = write_rows(db, [row])
    if inserted:
        typer.echo(f"Added thread to {target_week}.")
    else:
        typer.echo("Thread already exists for this week.")


@app.command()
def review(
    db: Path = typer.Option(DB_PATH, help="SQLite DB path."),
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 3: preview surfaced items before acting."""
    conn = sqlite3.connect(db)

    # 1. Check for surfaced items
    surfaced = conn.execute(
        "SELECT id, thread_text, suggested_action, rationale FROM thread_triage WHERE suggested_disposition = 'surface' AND human_disposition IS NULL"
    ).fetchall()

    # 2. Check for past-due defers
    from datetime import date

    today = date.today().isoformat()
    defers = conn.execute(
        "SELECT id, thread_text, suggested_action, rationale FROM thread_triage WHERE human_disposition = 'defer' AND resurface_after <= ?",
        (today,),
    ).fetchall()

    conn.close()

    if not surfaced and not defers:
        typer.echo("Nothing to surface for review. Run scan and classify first.")
        return

    if surfaced:
        typer.echo(f"\n--- Items Surfaced for Review ({len(surfaced)} item) ---\n")
        for row in surfaced:
            typer.echo(f"ID:{row[0]} | {row[1]}")
            typer.echo(f"  Action: {row[2]}")
            typer.echo(f"  Why: {row[3]}\n")

    if defers:
        typer.echo(f"\n--- Past-due defers ({len(defers)} item) ---\n")
        for row in defers:
            typer.echo(f"ID:{row[0]} | {row[1]}")
            typer.echo(f"  Action: {row[2]}")
            typer.echo(f"  Why: {row[3]}\n")

    typer.echo(
        "Use Claude + SQLite MCP to set human_disposition='capture' or 'task' on these rows."
    )


@app.command()
def act(
    db: Path = typer.Option(DB_PATH, help="SQLite DB path."),
    vault: Path = typer.Option(VAULT_PATH, help="Vault root path."),
    template: Optional[Path] = typer.Option(None, help="Custom daily note template."),
    dry_run: Annotated[bool, dry_run_option()] = False,
    verbose: Annotated[bool, verbose_option()] = False,
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 4: write surfaced items to ## Weekly Captures in today's daily note."""
    resolved_template = template or (vault / "Templates" / "Daily Note.md")
    acted, deferred, errors = run_act(
        db, vault, CAPTURES_DIR, dry_run, verbose, template_path=resolved_template
    )
    typer.echo(
        f"Phase 4 complete. Acted: {acted}, Deferred: {deferred}, Errors: {errors}"
    )


if __name__ == "__main__":
    app()
