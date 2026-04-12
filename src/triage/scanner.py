import re
from pathlib import Path
from datetime import date
from typing import Optional
from .schema import ThreadRow
from .config import THOUGHT_SECTIONS, SCAN_EXTENSIONS, SKIP_DIRS, SKIP_PATHS, SCAN_DIRS

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

def find_files_containing_dates(vault: Path, dates: list[date]) -> dict[Path, set[str]]:
    """Return a map of Path -> set of matching date strings.

    Only scans subdirectories listed in SCAN_DIRS (default: Timeline/).
    Everything else in the vault — project notes, templates, etc. — is ignored.
    """
    date_strings = {d.strftime("%Y-%m-%d") for d in dates}
    matches: dict[Path, set[str]] = {}

    scan_roots = [vault / d for d in SCAN_DIRS if (vault / d).is_dir()]
    if not scan_roots:
        return matches

    for root in scan_roots:
        for path in root.rglob("*"):
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

            # Strip frontmatter before date matching
            body = content
            if content.startswith("---"):
                end_idx = content.find("\n---", 3)
                if end_idx != -1:
                    body = content[end_idx + 4:]

            found = {ds for ds in date_strings if ds in body}
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

        # Unwrap Obsidian callout block content in thought sections
        effective_line = line
        if in_thought_section and re.match(r"^>", line):
            if re.match(r"^>\s*\[!", line):
                continue
            effective_line = re.sub(r"^>\s?", "", line)

        # Skip completed [x] and cancelled [-] task markers everywhere
        if re.match(r"^\s*[-*]\s+\[[-xX]\]", effective_line):
            continue

        # Unchecked tasks — only from thought sections
        task_match = re.match(r"^\s*-\s+\[ \]\s+(.+)", effective_line)
        if task_match:
            if in_thought_section:
                text = task_match.group(1).strip()
                if "🔁" in text or "🔄" in text:
                    continue
                if _meaningful_word_count(text) < 4:
                    continue
                if text:
                    threads.append(ThreadRow(
                        week="",  # filled in by caller
                        source_file=rel,
                        source_section=section,
                        thread_text=text,
                        thread_type="task",
                    ))
            continue

        # Thoughts — non-empty bullet lines in thought sections
        if in_thought_section:
            thought_match = re.match(r"^\s*[-*∙•·]\s+(.+)", effective_line)
            if thought_match:
                text = thought_match.group(1).strip()
                first_word = text.split()[0].lower().rstrip(";,(\\*") if text else ""
                if first_word in _SQL_KEYWORDS:
                    continue
                if "🔁" in text or "🔄" in text:
                    continue
                
                # Discovery metadata extraction: "... | term: #localai"
                search_term = None
                term_match = re.search(r"\|\s*term:\s*([^|]+)", text)
                if term_match:
                    search_term = term_match.group(1).strip()

                if text and len(text.split()) >= 4:
                    threads.append(ThreadRow(
                        week="",
                        source_file=rel,
                        source_section=section,
                        thread_text=text,
                        thread_type="thought",
                        search_term=search_term,
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
