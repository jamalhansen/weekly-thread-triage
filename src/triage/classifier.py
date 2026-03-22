import sqlite3
import typer
from pathlib import Path
from local_first_common.llm import parse_json_response
from local_first_common.tracking import timed_run
from .schema import Classification
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .db import build_context_payload

def classify_row(
    llm,
    row_id: int,
    thread_text: str,
    thread_type: str,
    context: str,
    system_prompt: str = SYSTEM_PROMPT,
) -> Classification:
    """Call LLM to classify a single thread row."""
    user = build_user_prompt(thread_text, thread_type, context)
    raw = llm.complete(system_prompt, user)
    data = parse_json_response(raw)
    return Classification(**data)

def run_classify(
    db: Path,
    llm,
    dry_run: bool,
    verbose: bool,
    personal_context: str = "",
    goal_context: str = "",
) -> int:
    """Run Phase 2: classify all pending rows. Returns count processed."""
    effective_prompt = SYSTEM_PROMPT
    if personal_context:
        effective_prompt = f"{SYSTEM_PROMPT}\n\n## Personal Context\n\n{personal_context}"
    if goal_context:
        effective_prompt = effective_prompt + "\n\n## Goal Context\n\n" + goal_context

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

        context = build_context_payload(conn)
        processed = 0

        with timed_run("weekly-thread-triage", getattr(llm, "model", None)) as _run:
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

            _run.item_count = processed
            _run.input_tokens = getattr(llm, "input_tokens", None) or None
            _run.output_tokens = getattr(llm, "output_tokens", None) or None

        return processed
    finally:
        conn.close()
