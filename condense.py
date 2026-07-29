"""Turn raw session notes into the markdown that becomes the Notion page body.

Contract — this is what the rest of the pipeline depends on, and it does not
change when the remaining prompt lands:

    in   raw_notes     verbatim Telegram messages, newline-joined, arrival order
         session_type  "class" | "floor"
    out  markdown string, used as the Notion page body as-is

Class sessions call Gemini 3.6 Flash on Vertex AI (see _invoke_llm). Floor
sessions are still a stub: prompts/floor.txt is a placeholder and floor notes
are echoed back unchanged, so the pipeline stays exercisable end to end until
that prompt is written.
"""

import os
from pathlib import Path

from google import genai
from google.genai import types

_PROMPT_DIR = Path(__file__).parent / "prompts"

# Loaded once at import (cold start), not per request — the files never change
# at runtime, and a missing file should fail the deploy's first request loudly
# rather than the first /done after a session.
_PROMPTS = {
    session_type: (_PROMPT_DIR / f"{session_type}.txt").read_text()
    for session_type in ("class", "floor")
}

_MODEL = "gemini-3.6-flash"

# "global" location sidesteps Vertex's per-region model availability — the
# thing that would otherwise force checking whether asia-south1 carries this
# model at all.
_client = genai.Client(
    vertexai=True,
    project=os.environ["VERTEX_PROJECT"],
    location=os.environ.get("VERTEX_LOCATION", "global"),
    # 100s vs. the function's 120s timeout: leaves ~20s to send the Telegram
    # error reply, so a slow model call is a caught exception instead of a
    # hard Cloud Functions kill that leaves the user with no response at all.
    http_options=types.HttpOptions(timeout=100_000),
)


def _invoke_llm(prompt: str, session_type: str) -> str:
    """Class sessions hit the model. Floor sessions still echo the notes back
    (prompts/floor.txt is a placeholder) until that prompt is written.
    """
    if session_type == "floor":
        raw_notes = prompt.split("--- PROMPT BEGINS ---", 1)[-1].strip()
        return (
            "## Floor barre — raw notes (not yet condensed)\n\n"
            f"{raw_notes}\n\n"
            "_Condensing prompt is still a placeholder; this page shows the notes as sent._"
        )

    response = _client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.2),
    )
    if not response.text:
        finish_reason = (
            response.candidates[0].finish_reason if response.candidates else "unknown"
        )
        raise RuntimeError(f"Model returned no text (finish_reason={finish_reason})")
    return response.text


def condense(raw_notes: str, session_type: str) -> str:
    template = _PROMPTS[session_type]
    prompt = template.replace("{RAW_NOTES}", raw_notes)
    return _invoke_llm(prompt, session_type)
