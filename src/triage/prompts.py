BATCH_SYSTEM_PROMPT = """You are a personal productivity assistant reviewing someone's weekly notes.

Your job: from the list of thoughts below, identify the 3-5 most worth surfacing — ideas or intentions
the person wrote down that have genuine action potential and might have been forgotten.

Exclude:
- Things already described as completed or done
- Pure observations with no action potential
- Vague notes with nothing concrete to act on
- Noise captured in the moment with no lasting value

If fewer than 3 items are worth surfacing, return only those. If nothing is worth surfacing, return an empty list.

Return JSON only:
{
  "items": [
    {
      "id": <integer ID from the input>,
      "suggested_action": "one concrete sentence describing what to do",
      "rationale": "one sentence explaining why this is worth acting on"
    }
  ]
}"""


def build_batch_user_prompt(
    rows: list[dict],
    personal_context: str = "",
    goal_context: str = "",
) -> str:
    lines = ["Here are the thoughts captured in my notes this week:", ""]
    for row in rows:
        lines.append(f"[ID:{row['id']}] {row['thread_text']}")

    if personal_context or goal_context:
        lines.append("")
        lines.append("## Context")
        if personal_context:
            lines.append(personal_context)
        if goal_context:
            lines.append(goal_context)

    return "\n".join(lines)
