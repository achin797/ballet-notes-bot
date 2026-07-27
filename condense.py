"""Turn raw session notes into the markdown that becomes the Notion page body.

Contract — this is what the rest of the pipeline depends on, and it does not
change when the real prompts land:

    in   raw_notes     verbatim Telegram messages, newline-joined, arrival order
         session_type  "class" | "floor"
    out  markdown string, used as the Notion page body as-is

STEP 1 IS A STUB. The prompt templates in prompts/ are placeholders and
_invoke_llm() does not call a model yet — it echoes the notes back so the whole
pipeline (buffer -> condense -> Notion) can be exercised end to end. Everything
around the model call is real: templates are loaded, {RAW_NOTES} is substituted,
and the result is what gets written. Filling in the prompts and swapping
_invoke_llm() for a Vertex AI call are the only two things left.
"""

from pathlib import Path

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Loaded once at import (cold start), not per request — the files never change
# at runtime, and a missing file should fail the deploy's first request loudly
# rather than the first /done of a session you just finished teaching.
_PROMPTS = {
    session_type: (_PROMPT_DIR / f"{session_type}.txt").read_text()
    for session_type in ("class", "floor")
}


def _invoke_llm(prompt: str, session_type: str) -> str:
    """STUB. Replace with a Vertex AI (Claude) call once the prompts are written.

    The replacement returns the model's reply text and nothing else — no
    formatting, no wrapping. Callers already treat the return value as final.
    """
    raw_notes = prompt.split("--- PROMPT BEGINS ---", 1)[-1].strip()
    label = "Class" if session_type == "class" else "Floor barre"
    return (
        f"## {label} — raw notes (not yet condensed)\n\n"
        f"{raw_notes}\n\n"
        f"_Condensing prompt is still a placeholder; this page shows the notes as sent._"
    )


def condense(raw_notes: str, session_type: str) -> str:
    template = _PROMPTS[session_type]
    prompt = template.replace("{RAW_NOTES}", raw_notes)
    return _invoke_llm(prompt, session_type)
