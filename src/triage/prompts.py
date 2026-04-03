DISPOSITION_CHOICES = "capture | task | defer | close | discard"

SYSTEM_PROMPT = """You are a productivity assistant helping classify open threads from a personal knowledge vault.

For each thread, suggest one of these dispositions:
- capture: this is a distinct idea worth turning into a tool spec, project note, or reference
- task: this is concrete work with a clear next action; add it to a task list
- defer: this is worth revisiting but not actionable this week
- close: this is already done, or no longer relevant
- discard: this is noise — captured in the moment, no lasting value

Be concise and decisive. One disposition per thread. One sentence for action and rationale."""

def build_user_prompt(thread_text: str, thread_type: str, context: str, search_term: str | None = None) -> str:
    discovery = f"Discovery context: Found via search term '{search_term}'" if search_term else ""
    return f"""Thread type: {thread_type}
{discovery}
Thread text: {thread_text}

{context}

Respond with JSON only:
{{
  "suggested_disposition": "{DISPOSITION_CHOICES}",
  "suggested_action": "one concrete sentence",
  "rationale": "one sentence explaining why"
}}"""
