# weekly-thread-triage

Scan the Obsidian vault for open threads from the past week and classify them for review.

Two headless phases write to SQLite; Phase 3 review happens in a Claude chat session via the SQLite MCP server.

## What it does

**Phase 1 — scan** (`thread_triage scan`):
Searches all vault files for date strings from the target week. Extracts unchecked tasks (`- [ ]`) and meaningful bullet points from thought sections (morning pages, reflections, voice journal). Writes new rows to the `thread_triage` table in `local-first.db`.

**Phase 2 — classify** (`thread_triage classify`):
Sends each unclassified row to an LLM with a prompt asking for a disposition (`capture | task | defer | close | discard`), a concrete suggested action, and a one-sentence rationale. Updates rows in place.

**Phase 3 — review** (Claude chat):
Open a chat session with the SQLite MCP server attached. Review rows, update `human_disposition`, and act on them.

## Installation

```bash
cd weekly-thread-triage
uv sync
```

Requires `local-first.db` to exist (see `~/.local-first/`). Create the `thread_triage` table via the SQLite MCP server or manually:

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

# Dry-run classification
uv run python thread_triage.py classify --dry-run --verbose
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
| `--dry-run` | `-n` | false | Show classifications without writing |
| `--verbose` | `-v` | false | Show row-by-row progress |
| `--debug` | `-d` | false | Show raw LLM prompts and responses |

## Environment variables

| Variable | Description |
|----------|-------------|
| `OBSIDIAN_VAULT_PATH` | Path to vault root (auto-detected if unset) |
| `LOCAL_FIRST_DB` | Path to `local-first.db` (default: `~/.local-first/local-first.db`) |
| `MODEL_PROVIDER` | Default LLM provider (`ollama`, `anthropic`, `gemini`, `groq`, `deepseek`) |

## Dispositions

| Value | Meaning |
|-------|---------|
| `capture` | Distinct idea → turn into a tool spec, project note, or reference |
| `task` | Concrete work with a clear next action → add to task list |
| `defer` | Worth revisiting but not actionable this week |
| `close` | Already done or no longer relevant |
| `discard` | Noise — captured in the moment, no lasting value |

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
