import os
from pathlib import Path
from local_first_common.obsidian import find_vault_root

# DB path resolution
_SYNC_DB   = Path("~/sync/thread-triage/thread-triage.db").expanduser()
_LEGACY_DB = Path("~/.local-first/local-first.db").expanduser()

def _resolve_db_path() -> Path:
    if explicit := os.environ.get("LOCAL_FIRST_DB"):
        return Path(explicit).expanduser()
    if _SYNC_DB.parent.exists():
        return _SYNC_DB
    return _LEGACY_DB

DB_PATH = _resolve_db_path()
VAULT_PATH = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "")).expanduser() or find_vault_root()

# Sections whose content counts as a "thought" rather than a task
THOUGHT_SECTIONS = {"morning pages", "thoughts", "voice journal", "reflections", "early morning"}

# File extensions to scan
SCAN_EXTENSIONS = {".md"}

# Directories to skip
SKIP_DIRS = {".obsidian", ".trash", "Templates", "_captures"}

# Path fragments to skip
SKIP_PATHS: set[str] = {
    p.strip()
    for p in os.environ.get("LOCAL_FIRST_SKIP_PATHS", "").split(":")
    if p.strip()
}

# Phase 4 — Act: where output lands in the vault
CAPTURES_DIR = os.environ.get("LOCAL_FIRST_CAPTURES_DIR", "_captures")

# Optional personal context file
CONTEXT_FILE = Path(
    os.environ.get("LOCAL_FIRST_THREAD_CONTEXT", "~/.local-first/thread-triage-context.md")
).expanduser()
