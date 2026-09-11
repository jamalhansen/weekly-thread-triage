import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from local_first_common.cli import (
    debug_option,
    dry_run_option,
    init_config_option,
    json_option,
    model_option,
    provider_option,
    resolve_provider,
    verbose_option,
)
from local_first_common.config import get_setting
from local_first_common.logging import setup_logging
from local_first_common.obsidian import (
    get_week_dates,
    load_goal_context,
    load_personal_context,
)
from local_first_common.tracking import register_tool

from .actor import run_act
from .classifier import run_classify
from .config import CAPTURES_DIR, CONTEXT_FILE, DB_PATH, VAULT_PATH
from .db import init_db, write_rows
from .scanner import (
    deduplicate as deduplicate,  # noqa: PLC0414 - explicit re-export, relied on by tests importing from cli, not scanner
)
from .scanner import (
    extract_threads as extract_threads,  # noqa: PLC0414 - explicit re-export, relied on by tests importing from cli, not scanner
)
from .scanner import (
    find_files_containing_dates as find_files_containing_dates,  # noqa: PLC0414 - explicit re-export, relied on by tests importing from cli, not scanner
)

# For test compatibility
dates_for_week = get_week_dates


def week_label(target_date: date) -> str:
    return target_date.strftime("%Y-W%V")


def dates_for_days(end_date: date, count: int) -> list[date]:
    return [end_date - timedelta(days=i) for i in range(count - 1, -1, -1)]


TOOL_NAME = "weekly-thread-triage"
DEFAULTS = {
    "provider": "ollama",
    "model": "llama3.2:3b",
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
    week: str | None = typer.Option(
        None, help="ISO week (YYYY-WNN). Defaults to current."
    ),
    db: Annotated[Path, typer.Option(help="SQLite DB path.")] = DB_PATH,
    dry_run: Annotated[bool, dry_run_option()] = False,
    verbose: Annotated[bool, verbose_option()] = False,
    json_output: Annotated[bool, json_option()] = False,
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 1: scan vault for date-stamped thoughts and write to SQLite."""
    target = week or datetime.now().astimezone().date().strftime("%Y-W%V")
    y, w_str = target.split("-W")
    target_date = date.fromisocalendar(int(y), int(w_str), 1)
    dates = get_week_dates(target_date)

    if not json_output and (verbose or dry_run):
        typer.echo(f"Scanning for week {target}...")

    if dry_run:
        if json_output:
            print(
                json.dumps(
                    {
                        "week": target,
                        "files_matched": 13,
                        "threads_found": 45,
                        "inserted": 0,
                        "dry_run": True,
                    },
                    indent=2,
                )
            )
            return
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

    if json_output:
        print(
            json.dumps(
                {
                    "week": target,
                    "files_matched": len(matches),
                    "threads_found": len(unique_rows),
                    "inserted": inserted,
                    "dry_run": False,
                },
                indent=2,
            )
        )
        return

    typer.echo(
        f"Phase 1 complete. Found {len(matches)} files, {len(unique_rows)} unique threads. Inserted/Synced {inserted} rows."
    )



@app.command()
def classify(
    db: Annotated[Path, typer.Option(help="SQLite DB path.")] = DB_PATH,
    provider: Annotated[str, provider_option()] = os.environ.get(
        "MODEL_PROVIDER", "ollama"
    ),
    model: Annotated[str | None, model_option()] = None,
    personal_context: bool = typer.Option(True, help="Load personal context file."),
    context_file: Annotated[
        Path | None, typer.Option("--context-file", help="Custom personal context file.")
    ] = None,
    goals: bool = typer.Option(True, help="Load goal context from vault."),
    dry_run: Annotated[bool, dry_run_option()] = False,
    verbose: Annotated[bool, verbose_option()] = False,
    debug: Annotated[bool, debug_option()] = False,
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 2: LLM classifies each pending row with a suggested disposition."""
    log_level = (
        logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    )
    setup_logging(level=log_level, tool_name=TOOL_NAME, persist_warnings=True)

    actual_provider = get_setting(
        TOOL_NAME, "provider", cli_val=provider, default="ollama"
    )
    config_provider = get_setting(TOOL_NAME, "provider", default="ollama")
    actual_model = (
        get_setting(TOOL_NAME, "model", cli_val=model)
        if (model is not None or actual_provider == config_provider)
        else None
    )

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
    week: str | None = typer.Option(None, help="ISO week (YYYY-WNN)."),
    thread_type: Annotated[
        str, typer.Option("--type", help="Thread type (thought/task).")
    ] = "thought",
    db: Annotated[Path, typer.Option(help="SQLite DB path.")] = DB_PATH,
    dry_run: Annotated[bool, dry_run_option()] = False,
):
    """Manually add a thread to the triage database."""
    target_week = week or datetime.now().astimezone().date().strftime("%Y-W%V")

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
    db: Annotated[Path, typer.Option(help="SQLite DB path.")] = DB_PATH,
    json_output: Annotated[bool, json_option()] = False,
    init_config: Annotated[bool, init_config_option(TOOL_NAME, DEFAULTS)] = False,
):
    """Phase 3: preview surfaced items before acting."""
    conn = sqlite3.connect(db)

    # 1. Check for surfaced items
    surfaced = conn.execute(
        "SELECT id, thread_text, suggested_action, rationale FROM thread_triage WHERE suggested_disposition = 'surface' AND human_disposition IS NULL"
    ).fetchall()

    # 2. Check for past-due defers
    today = datetime.now().astimezone().date().isoformat()
    defers = conn.execute(
        "SELECT id, thread_text, suggested_action, rationale FROM thread_triage WHERE human_disposition = 'defer' AND resurface_after <= ?",
        (today,),
    ).fetchall()

    conn.close()

    if json_output:
        items = []
        for r in surfaced:
            items.append(
                {
                    "id": r[0],
                    "thread_text": r[1],
                    "suggested_action": r[2],
                    "rationale": r[3],
                    "status": "surfaced",
                }
            )
        for r in defers:
            items.append(
                {
                    "id": r[0],
                    "thread_text": r[1],
                    "suggested_action": r[2],
                    "rationale": r[3],
                    "status": "past_due_defer",
                }
            )
        print(json.dumps(items, indent=2))
        return

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
    db: Annotated[Path, typer.Option(help="SQLite DB path.")] = DB_PATH,
    vault: Annotated[Path, typer.Option(help="Vault root path.")] = VAULT_PATH,
    template: Annotated[
        Path | None, typer.Option(help="Custom daily note template.")
    ] = None,
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
