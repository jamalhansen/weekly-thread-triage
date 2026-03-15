# weekly-thread-triage

Scan the Obsidian vault for open threads from the past week and classify them for review.

Two headless phases write to SQLite; Phase 3 review happens in a Claude chat session via the SQLite MCP server.

## What it does

**Phase 1 — scan** (`thread_triage scan`):
Searches all vault files for date strings from the target week. Extracts unchecked tasks (`- [ ]`) and meaningful bullet points from thought sections (morning pages, reflections, voice journal). Writes new rows to the `thread_triage` table in `local-first.db`.

**Phase 2 — classify** (`thread_triage classify`):
Sends each unclassified row to an LLM with a prompt asking for a disposition (`capture | task | defer | close | discard`), a concrete suggested action, and a one-sentence rationale. Updates rows in place. At classify time, the tool automatically loads your yearly and monthly goal files from the vault (`Goals/{year}/{year} Goals.md` and `Goals/{year}/_monthly/{year}-{month}.md`) and prepends them to the prompt so the LLM can align dispositions with your active goals. A personal context file can further improve quality (see [Personal context file](#personal-context-file)).

**Phase 3 — review** (Claude chat):
Open a chat session with the SQLite MCP server attached. Review rows and set `human_disposition` on each one (`capture | task | defer | close | discard`).

**Phase 4 — act** (`thread_triage act`):
Loops over all rows where `human_disposition IS NOT NULL AND executed_at IS NULL`. Creates an Obsidian note in `_captures/` for captures, appends a `- [ ]` task line to today's daily note `## Actions` section for tasks, and stamps `executed_at` when done. Defers are left untouched until a resurface mechanism is built.

## Installation

```bash
cd weekly-thread-triage
uv sync
```

The tool auto-discovers its DB at `~/sync/thread-triage/thread-triage.db` if that directory exists (ideal for iCloud/Dropbox sync). Otherwise it falls back to `~/.local-first/local-first.db`. Override with `LOCAL_FIRST_DB`.

To set up the sync location:

```bash
mkdir -p ~/sync/thread-triage
```

Create the `thread_triage` table via the SQLite MCP server or manually:

```sql
CREATE TABLE IF NOT EXISTS thread_triage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_section TEXT,
    thread_text TEXT NOT NULL,
    thread_type TEXT NOT NULL DEFAULT 'task',
    suggested_disposition TEXT,
    suggested_action TEXT,
    rationale TEXT,
    human_disposition TEXT,
    resurface_after TEXT,
    executed_at TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Usage

```bash
# Scan the current week
uv run python thread_triage.py scan

# Scan a specific ISO week
uv run python thread_triage.py scan --week 2026-W11

# Scan last 3 days instead of a full week
uv run python thread_triage.py scan --days 3

# Dry-run: see what would be written
uv run python thread_triage.py scan --dry-run --verbose

# Classify pending rows (default: ollama)
uv run python thread_triage.py classify

# Classify with Anthropic
uv run python thread_triage.py classify --provider anthropic

# Classify with a specific model
uv run python thread_triage.py classify --provider ollama --model llama3.2:3b

# Classify using a personal context file (strongly recommended — see below)
uv run python thread_triage.py classify --provider anthropic --context-file ~/.local-first/thread-triage-context.md

# Dry-run classification
uv run python thread_triage.py classify --dry-run --verbose

# Phase 3 helper — see what's pending review and any past-due defers
uv run python thread_triage.py review

# Filter to a specific week
uv run python thread_triage.py review --week 2026-W11

# Add a new row during Phase 3 review (no raw SQL needed)
uv run python thread_triage.py add "An idea I want to capture"
uv run python thread_triage.py add "Fix the scanner edge case" --type task --week 2026-W11

# Act on all reviewed rows (create notes, append tasks)
uv run python thread_triage.py act

# Preview what act would do without writing anything
uv run python thread_triage.py act --dry-run --verbose

# Override where capture notes land
uv run python thread_triage.py act --captures-dir "_captures"
```

## CLI reference

### `scan`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--week` | `-w` | current week | ISO week, e.g. `2026-W11` |
| `--days` | | — | Scan last N days instead of full week |
| `--db` | | `LOCAL_FIRST_DB` or `~/.local-first/local-first.db` | Path to SQLite DB |
| `--dry-run` | `-n` | false | Show what would be written without touching DB |
| `--verbose` | `-v` | false | Show progress detail |

### `classify`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--provider` | `-p` | `MODEL_PROVIDER` or `ollama` | LLM provider |
| `--model` | `-m` | provider default | Override provider's default model |
| `--db` | | `LOCAL_FIRST_DB` or `~/.local-first/local-first.db` | Path to SQLite DB |
| `--context-file` | `-c` | `LOCAL_FIRST_THREAD_CONTEXT` or `~/.local-first/thread-triage-context.md` | Personal context file prepended to the classify prompt |
| `--dry-run` | `-n` | false | Show classifications without writing |
| `--verbose` | `-v` | false | Show row-by-row progress |
| `--debug` | `-d` | false | Show raw LLM prompts and responses |

### `review`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--db` | | `LOCAL_FIRST_DB` or auto-discovered | Path to SQLite DB |
| `--week` | `-w` | — | Filter unreviewed rows to a specific ISO week |

### `add`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--db` | | `LOCAL_FIRST_DB` or auto-discovered | Path to SQLite DB |
| `--type` | `-t` | `thought` | Thread type: `thought` or `task` |
| `--week` | `-w` | current week | ISO week to assign the row to |
| `--source` | `-s` | `manual` | Source file reference shown in review output |
| `--dry-run` | `-n` | false | Show what would be added without writing |

### `act`

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--db` | | `LOCAL_FIRST_DB` or `~/.local-first/local-first.db` | Path to SQLite DB |
| `--captures-dir` | `-C` | `LOCAL_FIRST_CAPTURES_DIR` or `_captures` | Vault-relative folder for capture notes |
| `--dry-run` | `-n` | false | Show what would be created without writing |
| `--verbose` | `-v` | false | Show close/discard rows too |

## Environment variables

| Variable | Description |
|----------|-------------|
| `OBSIDIAN_VAULT_PATH` | Path to vault root (auto-detected if unset) |
| `LOCAL_FIRST_DB` | Explicit DB path override. Auto-discovered at `~/sync/thread-triage/thread-triage.db` if that directory exists, otherwise `~/.local-first/local-first.db` |
| `MODEL_PROVIDER` | Default LLM provider (`ollama`, `anthropic`, `gemini`, `groq`, `deepseek`) |
| `LOCAL_FIRST_THREAD_CONTEXT` | Path to personal context file (default: `~/.local-first/thread-triage-context.md`) |
| `LOCAL_FIRST_SKIP_PATHS` | Colon-separated path fragments to exclude from scanning (e.g. `_marketing:_strategy`) |
| `LOCAL_FIRST_CAPTURES_DIR` | Vault-relative folder for capture notes (default: `_captures`) |

## Dispositions

| Value | Meaning |
|-------|---------|
| `capture` | Distinct idea → turn into a tool spec, project note, or reference |
| `task` | Concrete work with a clear next action → add to task list |
| `defer` | Worth revisiting but not actionable this week — resurfaces next scan |
| `close` | Already done or no longer relevant |
| `discard` | Noise — captured in the moment, no lasting value |

## Goal context (automatic)

At classify time the tool automatically reads two goal files from the vault and prepends their content to the LLM prompt:

- `Goals/{year}/{year} Goals.md` — yearly goals
- `Goals/{year}/_monthly/{year}-{month}.md` — monthly focus

Obsidian frontmatter and wikilinks are stripped before sending. If neither file exists the classify prompt is unchanged. No configuration is needed — the files are discovered at runtime from `OBSIDIAN_VAULT_PATH`.

This allows the LLM to make goal-aligned suggestions: e.g. defer threads that conflict with this month's focus, or prefer `capture` for threads that extend an active project.

## Personal context file

The classify prompt is generic by default, which works but produces noisier results.
You can dramatically improve classification quality by providing a personal context file
that tells the LLM about your specific setup — without putting any of that private
information in the repo.

**The file lives at `~/.local-first/thread-triage-context.md`** (outside the repo, never
tracked by git). If it doesn't exist, classify works exactly as before.

A typical context file might include:

- Your active tool suite (so the LLM knows what "build a read-later queue" refers to)
- Workflow rules ("tasks in Actions sections are managed by the Obsidian Tasks plugin")
- Recurring patterns to recognise ("🔁 tasks are already tracked — mark as close")
- Your blog or project context (so the LLM can distinguish valuable ideas from noise)

```markdown
## Tool Suite

The following tools are already built and active:
- content-discovery-agent — scores RSS/Bluesky feeds
- transcription-summarizer — voice memo → Obsidian entries
...

## Task Management

Tasks in structured sections (Actions, etc.) are already managed by the Obsidian Tasks
plugin. Prefer `close` for these if they surface.

## Disposition Guide

- `capture` — a distinct idea worth a spec or blog post outline
- `task` — concrete work NOT already in the Tasks plugin
- `close` — done, shipped, or managed elsewhere
- `discard` — fleeting thought with no lasting value
```

Override the default path via env var or `--context-file`:

```bash
export LOCAL_FIRST_THREAD_CONTEXT="~/vaults/my-vault/triage-context.md"

# or per-run:
uv run python thread_triage.py classify --context-file ~/my-context.md
```

## Scanner behaviour

- **Frontmatter excluded from date matching** — files are matched by date strings in the note body only. A spec file with `Created: 2026-03-12` in YAML frontmatter is not included just because it was created this week.
- **`_captures` never rescanned** — Phase 1 skips the `_captures` directory so triage output notes don't feed back into next week's scan.
- **Recurring tasks filtered** — bullets containing `🔁` or `🔄` are skipped; they're already tracked by the Obsidian Tasks plugin.
- **Thought sections only** — unchecked tasks (`- [ ]`) are only extracted from thought sections (Morning Pages, Thoughts, Voice Journal, Reflections). Tasks in structured sections like `## Actions` are managed by the Tasks plugin and are left alone.

## Project structure

```
weekly-thread-triage/
├── thread_triage.py       # Phase 1 (scan) + Phase 2 (classify) CLI
├── tests/
│   ├── test_thread_triage.py
│   └── fixtures/
│       └── sample_daily_note.md
└── pyproject.toml
```

## Running tests

```bash
uv run pytest
```
