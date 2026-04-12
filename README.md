# weekly-thread-triage

Sunday morning tool. Scans your daily notes for thoughts from the past week, surfaces the 3-5 most worth acting on, and writes them to today's note as `## Weekly Captures`.

## Usage

```bash
uv run thread-triage scan
uv run thread-triage classify --provider anthropic
uv run thread-triage review          # optional: preview before writing
uv run thread-triage act
```

Run this before `weekly-review-generator`. The captures land in Sunday's daily note and weekly-review picks them up from there.

## Commands

**`scan`** — walks `Timeline/` for the current week, extracts bullets from Morning Pages and Thoughts sections, writes to SQLite. Only daily notes are scanned — project notes, templates, etc. are ignored.

**`classify`** — one batch LLM call receives all extracted thoughts and returns the 3-5 most worth surfacing. Everything else is marked discard.

**`review`** — shows what's queued and any past-due defers. Optional.

**`act`** — writes surfaced items to `## Weekly Captures` in today's daily note, with a source date on each item. Creates the note from your template if it doesn't exist.

## Key flags

| Command | Flag | Description |
|---------|------|-------------|
| `scan` | `--week 2026-W11` | Scan a specific week instead of current |
| `scan` | `--days 3` | Scan last N days |
| `scan` | `--dry-run` | Preview without writing |
| `classify` | `--provider` / `-p` | LLM provider (default: `ollama`) |
| `classify` | `--dry-run` | Show what would be selected |
| `act` | `--dry-run` | Preview the captures section |
| `act` | `--template` | Daily note template path (default: `vault/Templates/Daily Note.md`) |

## Environment variables

| Variable | Description |
|----------|-------------|
| `OBSIDIAN_VAULT_PATH` | Vault root (auto-detected if unset) |
| `LOCAL_FIRST_DB` | DB path override (default: `~/sync/thread-triage/thread-triage.db`) |
| `LOCAL_FIRST_SCAN_DIRS` | Vault subdirs to scan (default: `Timeline`) |
| `MODEL_PROVIDER` | Default LLM provider |

## Personal context file

`~/.local-first/thread-triage-context.md` — if it exists, its contents are prepended to the classify prompt. Useful for telling the LLM about your tool suite and what counts as signal vs. noise.

## Tests

```bash
uv run pytest
```
