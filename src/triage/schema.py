from pydantic import BaseModel
from typing import Optional
import re

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
