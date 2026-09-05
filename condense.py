"""Turn raw session notes into the markdown that becomes the Notion page body.

Contract — this is what the rest of the pipeline depends on:

    in   raw_notes     verbatim Telegram messages, newline-joined, arrival order
         session_type  "class" | "floor"
    out  markdown string, used as the Notion page body as-is

Both session types call Gemini 3.8 Flash on Vertex AI (see _invoke_llm); the
only difference between them is which prompt file gets loaded.

Voice notes go through transcribe() first, which turns audio into the same kind of
string a typed message would have been. Everything after that is identical — condense()
never learns whether the notes were spoken or typed.
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

# The vocabulary list is substituted into the transcription prompt once, here, rather
# than per request: neither file changes at runtime, so there's nothing to re-do.
_TRANSCRIBE_PROMPT = (_PROMPT_DIR / "transcribe.txt").read_text().replace(
    "{VOCAB}", (_PROMPT_DIR / "vocab.txt").read_text()
)

_MODEL = "gemini-3.8-flash"

# "global" location sidesteps Vertex's per-region model availability — the
# thing that would otherwise force checking whether asia-south1 carries this
# model at all.
_client = genai.Client(
    vertexai=True,
    project=os.environ["VERTEX_PROJECT"],
    location=os.environ.get("VERTEX_LOCATION", "global"),
    # 500s vs. the function's 540s timeout: leaves ~40s to send the Telegram
    # error reply, so a slow model call is a caught exception instead of a
    # hard Cloud Functions kill that leaves the user with no response at all.
    # Was 100s/120s, which a long voice note and a long /done both overran.
    http_options=types.HttpOptions(timeout=500_000),
)

# Telegram reports the same audio under several names depending on the client and
# on whether the note was recorded here or forwarded in. Map them onto the types
# Vertex actually accepts; an unrecognised value is passed through so the failure
# names the real mime type instead of a substituted one.
_MIME_ALIASES = {
    "audio/x-m4a": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/mpeg": "audio/mp3",
    "audio/mpga": "audio/mp3",
    "audio/x-wav": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/opus": "audio/ogg",
    "audio/oga": "audio/ogg",
}


def _invoke_llm(prompt: str) -> str:
    """Send the filled-in prompt to the model and return its markdown reply."""
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


def transcribe(audio: bytes, mime_type: str = "audio/ogg") -> str:
    """Turn a voice note into the text a typed message would have contained.

    Deliberately verbatim: the condensing prompts read hesitation and self-correction
    as signal (they drive the (watch) and (recurring) markers), so tidying the speech
    up here would quietly disarm those rules.

    Sent inline rather than via a bucket — Telegram caps a bot's download at 20MB,
    which is comfortably under the inline request limit, so a voice note always fits.
    """
    mime_type = _MIME_ALIASES.get(mime_type, mime_type)
    response = _client.models.generate_content(
        model=_MODEL,
        contents=[
            types.Part.from_bytes(data=audio, mime_type=mime_type),
            types.Part.from_text(text=_TRANSCRIBE_PROMPT),
        ],
        # 0.0, unlike condensing's 0.2: this is recognition, not writing. Sampling
        # freedom here shows up as ballet terms drifting into similar-sounding
        # English words.
        config=types.GenerateContentConfig(temperature=0.0),
    )
    if not response.text:
        finish_reason = (
            response.candidates[0].finish_reason if response.candidates else "unknown"
        )
        raise RuntimeError(f"Transcription returned no text (finish_reason={finish_reason})")
    return response.text


def condense(raw_notes: str, session_type: str) -> str:
    template = _PROMPTS[session_type]
    prompt = template.replace("{RAW_NOTES}", raw_notes)
    return _invoke_llm(prompt)
