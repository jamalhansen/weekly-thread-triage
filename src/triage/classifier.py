import sqlite3
import typer
from pathlib import Path
from local_first_common.llm import parse_json_response
from local_first_common.tracking import timed_run
from .prompts import BATCH_SYSTEM_PROMPT, build_batch_user_prompt


def run_classify(
    db: Path,
    llm,
    dry_run: bool,
    verbose: bool,
    personal_context: str = "",
    goal_context: str = "",
) -> int:
    """Run Phase 2: one batch LLM call selects 3-5 items to surface. Returns count selected."""
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
            typer.echo(f"[verbose] Sending {len(pending)} rows to LLM in one batch call...")

        rows = [{"id": r[0], "thread_text": r[1], "thread_type": r[2]} for r in pending]

        system = BATCH_SYSTEM_PROMPT
        if personal_context:
            system += f"\n\n## Personal Context\n\n{personal_context}"
        if goal_context:
            system += f"\n\n## Goal Context\n\n{goal_context}"

        user = build_batch_user_prompt(rows, personal_context, goal_context)

        with timed_run("weekly-thread-triage", getattr(llm, "model", None)) as _run:
            raw = llm.complete(system, user)
            data = parse_json_response(raw)

            selected = data.get("items", [])
            selected_map = {item["id"]: item for item in selected}
            selected_ids = set(selected_map.keys())

            if dry_run:
                typer.echo(f"\n[dry-run] Would surface {len(selected_ids)} of {len(pending)} items:")
                for item in selected:
                    typer.echo(f"  [ID:{item['id']}] {item.get('suggested_action', '')}")
                    typer.echo(f"    {item.get('rationale', '')}")
            else:
                for row_id, _, _ in pending:
                    if row_id in selected_ids:
                        info = selected_map[row_id]
                        conn.execute(
                            """UPDATE thread_triage
                               SET suggested_disposition = 'surface',
                                   suggested_action = ?,
                                   rationale = ?
                               WHERE id = ?""",
                            (info.get("suggested_action", ""), info.get("rationale", ""), row_id),
                        )
                        if verbose:
                            typer.echo(f"  [surface] ID:{row_id} — {info.get('suggested_action', '')[:70]}")
                    else:
                        conn.execute(
                            "UPDATE thread_triage SET suggested_disposition = 'discard' WHERE id = ?",
                            (row_id,),
                        )
                conn.commit()

            _run.item_count = len(selected_ids)
            _run.input_tokens = getattr(llm, "input_tokens", None) or None
            _run.output_tokens = getattr(llm, "output_tokens", None) or None

        return len(selected_ids)
    finally:
        conn.close()
