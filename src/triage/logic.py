"""Weekly Thread Triage — scan the vault for open threads and classify them for review.

Four phases:
  Phase 1 (scan)     — vault-wide date search, extract tasks/thoughts/ideas, write to SQLite
  Phase 2 (classify) — LLM classifies each pending row with a suggested disposition
  Phase 3 (review)   — Claude chat + SQLite MCP: set human_disposition on each row
  Phase 4 (act)      — create Obsidian notes for captures, append tasks, stamp executed_at
"""

import sqlite3
from datetime import date
from pathlib import Path
from typing import Annotated, Optional

import typer

from local_first_common.obsidian import get_week_dates
from local_first_common.providers import PROVIDERS
from local_first_common.cli import (
    resolve_provider,
    resolve_dry_run,
    dry_run_option,
    no_llm_option,
)
from local_first_common.text import strip_wikilinks

from .config import DB_PATH as DB_PATH, VAULT_PATH as VAULT_PATH, CAPTURES_DIR as CAPTURES_DIR, CONTEXT_FILE as CONTEXT_FILE, _resolve_db_path as _resolve_db_path
from .db import write_rows as write_rows, init_db as init_db
from .scanner import find_files_containing_dates as find_files_containing_dates, extract_threads as extract_threads, deduplicate as deduplicate
from .classifier import run_classify as run_classify
from .actor import run_act as run_act, append_task as append_task, create_capture_note as create_capture_note, slugify as slugify
from .schema import ThreadRow as ThreadRow

def load_personal_context(path: Path) -> str:
    """Return the personal context file contents, or '' if the file doesn't exist."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""

def load_goal_context(vault: Path, target_date: date) -> str:
    """Load yearly and monthly goal context from the vault."""
    year = target_date.year
    month = target_date.month
    sections: list[str] = []

    def _load(path: Path) -> str:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if content.startswith("---"):
            end = content.find("\n---", 3)
            if end != -1:
                content = content[end + 4:]
        return strip_wikilinks(content).strip()

    yearly = vault / "Goals" / str(year) / f"{year} Goals.md"
    if yearly.exists():
        try:
            sections.append(f"### {year} Yearly Goals\n\n{_load(yearly)}")
        except Exception:
            pass

    monthly = vault / "Goals" / str(year) / "_monthly" / f"{year}-{month:02d}.md"
    if monthly.exists():
        try:
            sections.append(f"### {year}-{month:02d} Monthly Focus\n\n{_load(monthly)}")
        except Exception:
            pass

    return "\n\n".join(sections)

def dates_for_week(target_date: date) -> list[date]:
    """Return all 7 dates in the ISO week containing target_date."""
    return get_week_dates(target_date)

def dates_for_days(n: int) -> list[date]:
    """Return the last N calendar dates including today."""
    from datetime import timedelta
    today = date.today()
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]

def week_label(target_date: date) -> str:
    """Return ISO week label e.g. '2026-W11'."""
    iso = target_date.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"

app = typer.Typer(help=__doc__)

@app.command()
def scan(
    week: Optional[str] = typer.Option(None, "--week", "-w", help="ISO week, e.g. 2026-W11"),
    days: Optional[int] = typer.Option(None, "--days", help="Scan last N days instead of full week"),
    db: Path = typer.Option(DB_PATH, help="Path to SQLite DB"),
    vault: Path = typer.Option(VAULT_PATH, help="Path to Obsidian vault"),
    dry_run: bool = dry_run_option(),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose."),
):
    """Phase 1: scan vault for date strings and extract thread candidates."""
    target_date = date.today()
    if week:
        # Simple parse: 2026-W11
        y, w = week.split("-W")
        # Find a date in that week
        target_date = date.fromisocalendar(int(y), int(w), 1)
    
    dates = dates_for_week(target_date)
    label = week_label(target_date)
    
    if days:
        dates = dates_for_days(days)
        label = f"last-{days}-days"

    if verbose:
        typer.echo(f"[verbose] Scanning vault: {vault}")
        typer.echo(f"[verbose] Date range: {dates[0]} \u2192 {dates[-1]} ({len(dates)} days)")

    files = find_files_containing_dates(vault, dates)
    if verbose:
        typer.echo(f"[verbose] Found {len(files)} files containing date strings")

    all_threads: list[ThreadRow] = []
    for path in files:
        rows = extract_threads(path, vault)
        for row in rows:
            row.week = label
        all_threads.extend(rows)

    unique = deduplicate(all_threads)
    if verbose:
        typer.echo(f"[verbose] Extracted {len(all_threads)} threads, {len(unique)} after deduplication")

    if dry_run:
        typer.echo(f"\n[dry-run] Would write {len(unique)} rows to {db}\n")
        for row in unique:
            typer.echo(f"  [{row.thread_type}] {row.source_file} / {row.source_section or '-'}")
            typer.echo(f"    {row.thread_text[:80]}{'...' if len(row.thread_text) > 80 else ''}")
        return

    if not dry_run:
        init_db(db)

    inserted = write_rows(db, unique)
    typer.echo(f"Phase 1 complete. New rows: {inserted}, Duplicates skipped: {len(unique) - inserted}")

@app.command()
def classify(
    provider: Annotated[str, typer.Option("--provider", "-p", help="LLM provider.")] = "ollama",
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model name."),
    db: Path = typer.Option(DB_PATH, help="Path to SQLite DB"),
    context_file: Path = typer.Option(CONTEXT_FILE, "--context-file", "-c", help="Personal context file"),
    dry_run: bool = dry_run_option(),
    no_llm: bool = no_llm_option(),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose."),
    debug: bool = typer.Option(False, "--debug", "-d", help="Debug."),
):
    """Phase 2: use an LLM to suggest dispositions for pending rows."""
    dry_run = resolve_dry_run(dry_run, no_llm)
    llm = resolve_provider(PROVIDERS, provider, model, debug=debug, no_llm=no_llm)
    
    personal_context = load_personal_context(context_file)
    goal_context = load_goal_context(VAULT_PATH, date.today())
    
    if not dry_run:
        init_db(db)

    if verbose and personal_context:
        typer.echo(f"[verbose] Personal context loaded from {context_file}")
    if verbose and goal_context:
        typer.echo("[verbose] Goal context loaded from vault")

    count = run_classify(
        db, llm, dry_run, verbose,
        personal_context=personal_context,
        goal_context=goal_context
    )
    typer.echo(f"Phase 2 complete. Processed {count} rows.")

@app.command()
def review(
    db: Path = typer.Option(DB_PATH, help="Path to SQLite DB"),
    week: Optional[str] = typer.Option(None, "--week", "-w", help="Filter to ISO week"),
    term: Optional[str] = typer.Option(None, "--term", "-t", help="Filter by discovery search term"),
):
    """Phase 3 helper: show rows pending human review."""
    init_db(db)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    
    query = "SELECT id, week, source_file, thread_text, search_term FROM thread_triage WHERE human_disposition IS NULL"
    params = []
    if week:
        query += " AND week = ?"
        params.append(week)
    if term:
        query += " AND search_term LIKE ?"
        params.append(f"%{term}%")
    
    rows = conn.execute(query, params).fetchall()
    
    # Past-due defers
    defer_query = """SELECT id, week, source_file, thread_text, resurface_after, search_term
           FROM thread_triage
           WHERE human_disposition = 'defer'
             AND executed_at IS NULL
             AND resurface_after IS NOT NULL
             AND resurface_after <= date('now')"""
    defer_params = []
    if week:
        defer_query += " AND week = ?"
        defer_params.append(week)
    if term:
        defer_query += " AND search_term LIKE ?"
        defer_params.append(f"%{term}%")
        
    defer_query += " ORDER BY resurface_after"
    defers = conn.execute(defer_query, defer_params).fetchall()

    if defers:
        typer.echo(f"\n!  Past-due defers ({len(defers)}) \u2014 ready to resurface:\n")
        for r in defers:
            term_str = f" | term: {r['search_term']}" if r['search_term'] else ""
            typer.echo(f"  [{r['id']}] due {r['resurface_after']} | {r['week']} | {r['source_file']}{term_str}")
            typer.echo(f"    {r['thread_text'][:80]}{'...' if len(r['thread_text']) > 80 else ''}")

    if not rows:
        if not defers:
            typer.echo("No rows pending review.")
        return

    from rich.console import Console
    from rich.table import Table
    console = Console()
    table = Table(title="Pending Review")
    table.add_column("ID", justify="right", style="cyan")
    table.add_column("Week", style="magenta")
    table.add_column("Term", style="yellow")
    table.add_column("Source", style="green")
    table.add_column("Text")

    for r in rows:
        table.add_row(str(r["id"]), r["week"], r["search_term"] or "-", r["source_file"], r["thread_text"][:100])
    
    console.print(table)
    typer.echo(f"\nTotal: {len(rows)} rows pending review.")
    typer.echo("Use SQLite MCP or direct SQL to set human_disposition.")

@app.command()
def add(
    text: str = typer.Argument(..., help="The thread text to add"),
    type: Annotated[str, typer.Option("--type", "-t", help="Thread type: task or thought")] = "thought",
    week: Optional[str] = typer.Option(None, "--week", "-w", help="ISO week to assign to"),
    source: Annotated[str, typer.Option("--source", "-s", help="Source reference")] = "manual",
    db: Path = typer.Option(DB_PATH, help="Path to SQLite DB"),
    dry_run: bool = dry_run_option(),
):
    """Add a new thread row manually."""
    label = week or week_label(date.today())
    row = ThreadRow(label, source, "manual", text, type)
    
    if dry_run:
        typer.echo(f"[dry-run] Would add: [{type}] {text} to week {label}")
        return

    init_db(db)
    inserted = write_rows(db, [row])
    if inserted:
        typer.echo(f"Added manually to week {label}")
    else:
        typer.echo("Row already exists.")

@app.command()
def act(
    db: Path = typer.Option(DB_PATH, help="Path to SQLite DB"),
    vault: Path = typer.Option(VAULT_PATH, help="Path to Obsidian vault"),
    captures_dir: str = typer.Option(CAPTURES_DIR, "--captures-dir", "-C", help="Vault folder for captures"),
    dry_run: bool = dry_run_option(),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose."),
):
    """Phase 4: execute actions for all reviewed rows."""
    if dry_run:
        typer.echo("[dry-run] Would execute actions...")
    acted, deferred, errors = run_act(db, vault, captures_dir, dry_run, verbose)
    typer.echo(f"Phase 4 complete. Acted: {acted}, Deferred: {deferred}, Errors: {errors}")

if __name__ == "__main__":
    app()
