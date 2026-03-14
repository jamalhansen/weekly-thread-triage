"""Weekly Thread Triage — scan the vault for open threads and classify them for review.

Four phases:
  Phase 1 (scan)     — vault-wide date search, extract tasks/thoughts/ideas, write to SQLite
  Phase 2 (classify) — LLM classifies each pending row with a suggested disposition
  Phase 3 (review)   — Claude chat + SQLite MCP: set human_disposition on each row
  Phase 4 (act)      — create Obsidian notes for captures, append tasks, stamp executed_at
"""

import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Optional

import typer
from pydantic import BaseModel

from local_first_common.obsidian import find_vault_root, get_week_dates
from local_first_common.providers import PROVIDERS
from local_first_common.cli import resolve_provider

# ── Config ────────────────────────────────────────────────────────────────────

# DB path resolution — priority: env var → sync location → legacy default.
# ~/sync/thread-triage/ is auto-discovered when the directory exists, making it
# easy to place the DB in a cloud-synced folder (iCloud, Dropbox, etc.) without
# any env var configuration.
_SYNC_DB   = Path("~/sync/thread-triage/thread-triage.db").expanduser()
_LEGACY_DB = Path("~/.local-first/local-first.db").expanduser()


def _resolve_db_path() -> Path:
    """Resolve the SQLite DB path.

    Priority:
    1. LOCAL_FIRST_DB env var — explicit override for any path
    2. ~/sync/thread-triage/thread-triage.db — auto-discovered if directory exists
    3. ~/.local-first/local-first.db — legacy default
    """
    if explicit := os.environ.get("LOCAL_FIRST_DB"):
        return Path(explicit).expanduser()
    if _SYNC_DB.parent.exists():
        return _SYNC_DB
    return _LEGACY_DB


DB_PATH = _resolve_db_path()
VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "")).expanduser() or find_vault_root()

# Sections whose content counts as a "thought" rather than a task
THOUGHT_SECTIONS = {"morning pages", "thoughts", "voice journal", "reflections"}

# File extensions to scan
SCAN_EXTENSIONS = {".md"}

# Files/dirs to skip even if they contain date strings
SKIP_DIRS = {".obsidian", ".trash", "Templates"}

# Path fragments to skip (colon-separated in LOCAL_FIRST_SKIP_PATHS env var)
# e.g. LOCAL_FIRST_SKIP_PATHS="_marketing:_strategy:Local-First AI - Prep Timeline"
SKIP_PATHS: set[str] = {
    p.strip()
    for p in os.environ.get("LOCAL_FIRST_SKIP_PATHS", "").split(":")
    if p.strip()
}

# Phase 4 — Act: where output lands in the vault
# Both are vault-relative paths; override via env vars.
CAPTURES_DIR = os.environ.get("LOCAL_FIRST_CAPTURES_DIR", "_captures")
TASKS_FILE   = os.environ.get("LOCAL_FIRST_TASKS_FILE",   "_captures/_CAPTURED_TASKS.md")

# Optional personal context file — prepended to the classify system prompt at runtime.
# Lives outside the repo so private details (tool names, workflows) never hit git.
# Override path with LOCAL_FIRST_THREAD_CONTEXT env var; see README for format.
CONTEXT_FILE = Path(
    os.environ.get("LOCAL_FIRST_THREAD_CONTEXT", "~/.local-first/thread-triage-context.md")
).expanduser()


def load_personal_context(path: Path) -> str:
    """Return the personal context file contents, or '' if the file doesn't exist."""
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


# First words that indicate a line is code/SQL, not a natural-language thought
_SQL_KEYWORDS = {"select", "insert", "update", "delete", "create", "drop", "alter", "with"}

# Obsidian task metadata patterns to strip before checking meaningful word count
_TASK_METADATA_RE = re.compile(
    r"📅\s*\d{4}-\d{2}-\d{2}"   # due date
    r"|⏳\s*\d{4}-\d{2}-\d{2}"  # scheduled date
    r"|✅\s*\d{4}-\d{2}-\d{2}"  # done date
    r"|\[\[.*?\]\]"              # Obsidian wiki links
    r"|https?://\S+"             # URLs
    r"|#\w+"                     # tags
)


def _meaningful_word_count(text: str) -> int:
    """Count words remaining after stripping Obsidian task metadata and emojis."""
    stripped = _TASK_METADATA_RE.sub(" ", text)
    stripped = re.sub(r"[^\w\s]", " ", stripped)  # emojis, remaining punctuation
    return len(stripped.split())

app = typer.Typer(help=__doc__)


# ── Models ────────────────────────────────────────────────────────────────────

class ThreadRow:
    """A candidate thread extracted from a vault file."""
    def __init__(
        self,
        week: str,
        source_file: str,
        source_section: Optional[str],
        thread_text: str,
        thread_type: str,
    ):
        self.week = week
        self.source_file = source_file
        self.source_section = source_section
        self.thread_text = thread_text.strip()
        self.thread_type = thread_type

    def dedup_key(self) -> str:
        """Normalised text used to detect duplicates across files."""
        return re.sub(r"\s+", " ", self.thread_text.lower().strip())


class Classification(BaseModel):
    suggested_disposition: str   # capture | task | defer | close | discard
    suggested_action: str        # one concrete sentence
    rationale: str               # one sentence why


# ── Phase 1: Scan ─────────────────────────────────────────────────────────────

def week_label(target_date: date) -> str:
    """Return ISO week label e.g. '2026-W11'."""
    iso = target_date.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def dates_for_week(target_date: date) -> list[date]:
    """Return all 7 dates in the ISO week containing target_date."""
    return get_week_dates(target_date)


def dates_for_days(n: int) -> list[date]:
    """Return the last N calendar dates including today."""
    today = date.today()
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


def find_files_containing_dates(vault: Path, dates: list[date]) -> dict[Path, set[str]]:
    """
    Search vault-wide for files that contain any of the given date strings.
    Returns {file_path: {matched_date_strings}}.
    Skips hidden dirs, Templates, and .trash.
    """
    date_strings = {d.strftime("%Y-%m-%d") for d in dates}
    matches: dict[Path, set[str]] = {}

    for path in vault.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in SCAN_EXTENSIONS:
            continue
        if any(skip in path.parts for skip in SKIP_DIRS):
            continue

        rel = str(path.relative_to(vault))
        if SKIP_PATHS and any(skip in rel for skip in SKIP_PATHS):
            continue

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        found = {ds for ds in date_strings if ds in content}
        if found:
            matches[path] = found

    return matches


def current_section(lines: list[str], line_idx: int) -> Optional[str]:
    """Walk backwards from line_idx to find the most recent ## heading."""
    for i in range(line_idx - 1, -1, -1):
        m = re.match(r"^#{1,3}\s+(.+)", lines[i])
        if m:
            return m.group(1).strip()
    return None


def extract_threads(path: Path, vault: Path) -> list[ThreadRow]:
    """Extract tasks, thoughts, and ideas from a single file."""
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    lines = content.splitlines()
    threads: list[ThreadRow] = []
    rel = str(path.relative_to(vault))

    # Strip frontmatter
    start = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() == "---":
                start = i + 1
                break

    for i, line in enumerate(lines[start:], start):
        section = current_section(lines, i)
        section_lower = section.lower() if section else ""
        in_thought_section = any(s in section_lower for s in THOUGHT_SECTIONS)

        # Skip completed [x] and cancelled [-] task markers everywhere
        if re.match(r"^\s*[-*]\s+\[[-xX]\]", line):
            continue

        # Unchecked tasks — only from thought sections (other sections are managed by
        # the Obsidian Tasks plugin and don't need surfacing here)
        task_match = re.match(r"^\s*-\s+\[ \]\s+(.+)", line)
        if task_match:
            if in_thought_section:
                text = task_match.group(1).strip()
                if "🔁" in text:
                    continue  # recurring — already tracked
                if _meaningful_word_count(text) < 3:
                    continue  # fragment with no real content
                if text:
                    threads.append(ThreadRow(
                        week="",  # filled in by caller
                        source_file=rel,
                        source_section=section,
                        thread_text=text,
                        thread_type="task",
                    ))
            continue  # always skip thought check for task-marker lines

        # Thoughts — non-empty bullet lines in thought sections
        if in_thought_section:
            thought_match = re.match(r"^\s*[-*]\s+(.+)", line)
            if thought_match:
                text = thought_match.group(1).strip()
                first_word = text.split()[0].lower().rstrip(";,(\\*") if text else ""
                if first_word in _SQL_KEYWORDS:
                    continue
                if "🔁" in text:
                    continue  # recurring reminder, not a thread
                if text and len(text.split()) >= 4:
                    threads.append(ThreadRow(
                        week="",
                        source_file=rel,
                        source_section=section,
                        thread_text=text,
                        thread_type="thought",
                    ))

    return threads


def deduplicate(rows: list[ThreadRow]) -> list[ThreadRow]:
    """Remove rows with identical normalised text, keeping first occurrence."""
    seen: set[str] = set()
    unique: list[ThreadRow] = []
    for row in rows:
        key = row.dedup_key()
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique


def write_rows(db: Path, rows: list[ThreadRow]) -> int:
    """Insert rows into thread_triage, skipping duplicates.

    Skips a row if:
    - The same text already exists for this week (same-week dedup), OR
    - The same text was already actioned in a prior week (human_disposition IS NOT NULL)

    Returns count of new rows inserted.
    """
    conn = sqlite3.connect(db)
    inserted = 0
    try:
        for row in rows:
            # Same-week dedup: don't insert twice for the same week
            existing = conn.execute(
                "SELECT id FROM thread_triage WHERE week = ? AND thread_text = ?",
                (row.week, row.thread_text),
            ).fetchone()
            if existing:
                continue

            # Cross-week dedup: skip if already actioned in a prior week.
            # Deferred rows are excluded — they resurface each week until the
            # user assigns a final disposition (capture/task/close/discard).
            prior_actioned = conn.execute(
                """SELECT id FROM thread_triage
                   WHERE thread_text = ? AND human_disposition IS NOT NULL
                     AND human_disposition != 'defer' AND week != ?""",
                (row.thread_text, row.week),
            ).fetchone()
            if prior_actioned:
                continue

            conn.execute(
                """INSERT INTO thread_triage
                   (week, source_file, source_section, thread_text, thread_type)
                   VALUES (?, ?, ?, ?, ?)""",
                (row.week, row.source_file, row.source_section, row.thread_text, row.thread_type),
            )
            inserted += 1
        conn.commit()
    finally:
        conn.close()
    return inserted


def run_scan(
    dates: list[date],
    week: str,
    vault: Path,
    db: Path,
    dry_run: bool,
    verbose: bool,
) -> list[ThreadRow]:
    """Run Phase 1: scan vault, extract threads, write to DB."""
    if verbose:
        typer.echo(f"[verbose] Scanning vault: {vault}")
        typer.echo(f"[verbose] Date range: {dates[0]} → {dates[-1]} ({len(dates)} days)")

    files = find_files_containing_dates(vault, dates)
    if verbose:
        typer.echo(f"[verbose] Found {len(files)} files containing date strings")

    all_threads: list[ThreadRow] = []
    for path in files:
        rows = extract_threads(path, vault)
        for row in rows:
            row.week = week
        all_threads.extend(rows)

    unique = deduplicate(all_threads)
    if verbose:
        typer.echo(f"[verbose] Extracted {len(all_threads)} threads, {len(unique)} after deduplication")

    if dry_run:
        typer.echo(f"\n[dry-run] Would write {len(unique)} rows to {db}\n")
        for row in unique:
            typer.echo(f"  [{row.thread_type}] {row.source_file} / {row.source_section or '—'}")
            typer.echo(f"    {row.thread_text[:80]}{'…' if len(row.thread_text) > 80 else ''}")
        return unique

    inserted = write_rows(db, unique)
    typer.echo(f"Phase 1 complete. New rows: {inserted}, Duplicates skipped: {len(unique) - inserted}")
    return unique


# ── Phase 2: Classify ─────────────────────────────────────────────────────────

DISPOSITION_CHOICES = "capture | task | defer | close | discard"

SYSTEM_PROMPT = """You are a productivity assistant helping classify open threads from a personal knowledge vault.

For each thread, suggest one of these dispositions:
- capture: this is a distinct idea worth turning into a tool spec, project note, or reference
- task: this is concrete work with a clear next action; add it to a task list
- defer: this is worth revisiting but not actionable this week
- close: this is already done, or no longer relevant
- discard: this is noise — captured in the moment, no lasting value

Be concise and decisive. One disposition per thread. One sentence for action and rationale."""


def build_context_payload(conn: sqlite3.Connection) -> str:
    """Build a compact context string to ground the LLM's classifications."""
    # Recent dispositions (avoid re-suggesting already-handled things)
    recent = conn.execute(
        """SELECT thread_text, human_disposition FROM thread_triage
           WHERE human_disposition IS NOT NULL
           ORDER BY created_at DESC LIMIT 10"""
    ).fetchall()

    lines = ["Recent dispositions (for context — avoid re-suggesting these):"]
    for text, disp in recent:
        lines.append(f"  [{disp}] {text[:60]}")

    return "\n".join(lines) if recent else ""


def classify_row(
    llm,
    row_id: int,
    thread_text: str,
    thread_type: str,
    context: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> Classification:
    """Call LLM to classify a single thread row."""
    import json

    user = f"""Thread type: {thread_type}
Thread text: {thread_text}

{context}

Respond with JSON only:
{{
  "suggested_disposition": "{DISPOSITION_CHOICES}",
  "suggested_action": "one concrete sentence",
  "rationale": "one sentence explaining why"
}}"""

    raw = llm.complete(system_prompt, user)
    raw = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    raw = re.sub(r"\n?```$", "", raw.strip())
    data = json.loads(raw)
    return Classification(**data)


def run_classify(
    db: Path,
    llm,
    dry_run: bool,
    verbose: bool,
    context_file: Path = CONTEXT_FILE,
) -> int:
    """Run Phase 2: classify all pending rows. Returns count processed."""
    personal_context = load_personal_context(context_file)
    effective_prompt = SYSTEM_PROMPT
    if personal_context:
        effective_prompt = f"{SYSTEM_PROMPT}\n\n## Personal Context\n\n{personal_context}"

    conn = sqlite3.connect(db)
    try:
        pending = conn.execute(
            """SELECT id, thread_text, thread_type FROM thread_triage
               WHERE suggested_disposition IS NULL
               ORDER BY created_at"""
        ).fetchall()

        if not pending:
            typer.echo("No pending rows to classify.")
            return 0

        if verbose:
            typer.echo(f"[verbose] Classifying {len(pending)} pending rows...")
            if personal_context:
                typer.echo(f"[verbose] Personal context loaded from {context_file} ({len(personal_context)} chars)")
            else:
                typer.echo(f"[verbose] No personal context file found at {context_file}")

        context = build_context_payload(conn)
        processed = 0

        for row_id, thread_text, thread_type in pending:
            try:
                result = classify_row(
                    llm, row_id, thread_text, thread_type or "thought", context,
                    system_prompt=effective_prompt,
                )
            except Exception as e:
                typer.echo(f"  [error] Row {row_id}: {e}", err=True)
                continue

            if dry_run:
                typer.echo(f"\n[dry-run] Row {row_id}: {thread_text[:60]}…")
                typer.echo(f"  disposition: {result.suggested_disposition}")
                typer.echo(f"  action: {result.suggested_action}")
                typer.echo(f"  rationale: {result.rationale}")
            else:
                conn.execute(
                    """UPDATE thread_triage
                       SET suggested_disposition = ?, suggested_action = ?, rationale = ?
                       WHERE id = ?""",
                    (result.suggested_disposition, result.suggested_action, result.rationale, row_id),
                )
                conn.commit()
                if verbose:
                    typer.echo(f"  Row {row_id}: {result.suggested_disposition} — {result.suggested_action}")

            processed += 1

        return processed
    finally:
        conn.close()


# ── Phase 4: Act ──────────────────────────────────────────────────────────────

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
    tasks_file: str,
    dry_run: bool,
) -> Path:
    """Append a task line to the tasks file. Returns the target path."""
    tasks_path = vault / tasks_file
    task_line = f"\n- [ ] {suggested_action} ([[{source_file}]])\n"

    if not dry_run:
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        with tasks_path.open("a", encoding="utf-8") as f:
            f.write(task_line)

    return tasks_path


def run_act(
    db: Path,
    vault: Path,
    captures_dir: str,
    tasks_file: str,
    dry_run: bool,
    verbose: bool,
) -> tuple[int, int, int]:
    """Run Phase 4: act on all reviewed rows that haven't been executed yet.

    - capture  → create an Obsidian note in captures_dir
    - task     → append a task line to tasks_file
    - close / discard → stamp executed_at, no note needed
    - defer    → skip (no resurface mechanism yet)

    Returns (acted, deferred, errors).
    """
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
                        vault, tasks_file, dry_run,
                    )
                    typer.echo(f"  [task] → {tasks_file}")

                elif disp in ("close", "discard"):
                    if verbose:
                        typer.echo(f"  [{disp}] {thread_text[:70]}…")

                elif disp == "defer":
                    deferred += 1
                    if not dry_run:
                        conn.execute(
                            "UPDATE thread_triage SET resurface_after = date('now', '+7 days') WHERE id = ?",
                            (row_id,),
                        )
                    if verbose:
                        typer.echo(f"  [defer] resurface after 7 days: {thread_text[:60]}…")
                    continue  # leave executed_at NULL

                else:
                    if verbose:
                        typer.echo(f"  [unknown:{disp}] {thread_text[:70]}…")

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
    finally:
        conn.close()

    return acted, deferred, errors


# ── CLI ───────────────────────────────────────────────────────────────────────

@app.command()
def scan(
    week: Annotated[
        Optional[str],
        typer.Option("--week", "-w", help="ISO week to scan, e.g. 2026-W11. Defaults to current week."),
    ] = None,
    days: Annotated[
        Optional[int],
        typer.Option("--days", help="Scan last N days instead of a full ISO week."),
    ] = None,
    db: Annotated[
        str,
        typer.Option("--db", help="Path to local-first.db. Defaults to LOCAL_FIRST_DB env var or ~/.local-first/local-first.db."),
    ] = str(DB_PATH),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be written without touching the DB."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show progress detail."),
    ] = False,
):
    """Phase 1 — scan vault for open threads and write to SQLite."""
    db_path = Path(db).expanduser()

    if not dry_run and not db_path.exists():
        typer.echo(f"Error: DB not found at {db_path}. Create it first or set LOCAL_FIRST_DB.", err=True)
        raise typer.Exit(1)

    if days:
        dates = dates_for_days(days)
        label = week_label(dates[-1])
    else:
        target = date.today()
        if week:
            # Parse YYYY-WNN
            m = re.match(r"(\d{4})-W(\d{1,2})", week)
            if not m:
                typer.echo("Error: --week must be in format YYYY-WNN, e.g. 2026-W11", err=True)
                raise typer.Exit(1)
            year, wnum = int(m.group(1)), int(m.group(2))
            # Get the Monday of that week
            target = date.fromisocalendar(year, wnum, 1)
            label = week
        else:
            label = week_label(target)
        dates = dates_for_week(target)

    run_scan(dates, label, VAULT_PATH, db_path, dry_run, verbose)
    if not dry_run:
        typer.echo(f"\nDone. Processed: 1, Skipped: 0")


@app.command()
def classify(
    db: Annotated[
        str,
        typer.Option("--db", help="Path to local-first.db."),
    ] = str(DB_PATH),
    provider: Annotated[
        str,
        typer.Option("--provider", "-p", help=f"LLM provider. Choices: {', '.join(PROVIDERS.keys())}"),
    ] = os.environ.get("MODEL_PROVIDER", "ollama"),
    model: Annotated[
        Optional[str],
        typer.Option("--model", "-m", help="Override the provider's default model."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show classifications without writing to DB."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show progress detail."),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option("--debug", "-d", help="Show raw LLM prompts and responses."),
    ] = False,
    context_file: Annotated[
        str,
        typer.Option("--context-file", "-c", help="Path to personal context .md file prepended to the classify prompt. Defaults to LOCAL_FIRST_THREAD_CONTEXT or ~/.local-first/thread-triage-context.md."),
    ] = str(CONTEXT_FILE),
):
    """Phase 2 — classify pending rows with the LLM."""
    db_path = Path(db).expanduser()

    if not db_path.exists():
        typer.echo(f"Error: DB not found at {db_path}.", err=True)
        raise typer.Exit(1)

    try:
        llm = resolve_provider(PROVIDERS, provider, model, debug=debug)
    except (RuntimeError, ValueError) as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    processed = run_classify(db_path, llm, dry_run, verbose, context_file=Path(context_file).expanduser())
    typer.echo(f"\nDone. Processed: {processed}, Skipped: 0")


@app.command()
def act(
    db: Annotated[
        str,
        typer.Option("--db", help="Path to local-first.db."),
    ] = str(DB_PATH),
    captures_dir: Annotated[
        str,
        typer.Option("--captures-dir", "-C", help="Vault-relative folder for capture notes. Defaults to LOCAL_FIRST_CAPTURES_DIR or '_captures'."),
    ] = CAPTURES_DIR,
    tasks_file: Annotated[
        str,
        typer.Option("--tasks-file", "-t", help="Vault-relative path of the tasks file to append to. Defaults to LOCAL_FIRST_TASKS_FILE or '_captures/_CAPTURED_TASKS.md'."),
    ] = TASKS_FILE,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be created/written without touching files or the DB."),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show per-row detail including close/discard."),
    ] = False,
):
    """Phase 4 — act on reviewed rows: create capture notes, append tasks, stamp executed_at."""
    db_path = Path(db).expanduser()

    if not db_path.exists():
        typer.echo(f"Error: DB not found at {db_path}.", err=True)
        raise typer.Exit(1)

    if not dry_run and not VAULT_PATH.exists():
        typer.echo(f"Error: Vault not found at {VAULT_PATH}. Set OBSIDIAN_VAULT_PATH.", err=True)
        raise typer.Exit(1)

    acted, deferred, errors = run_act(db_path, VAULT_PATH, captures_dir, tasks_file, dry_run, verbose)

    suffix = " (dry-run)" if dry_run else ""
    typer.echo(f"\nDone{suffix}. Acted: {acted}, Deferred (skipped): {deferred}, Errors: {errors}")


if __name__ == "__main__":
    app()
