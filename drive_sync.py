"""Rebuild the Google Docs that back the NotebookLM notebook.

One Doc per Notion database, regenerated in full on every run. There is no
per-page state and nothing incremental: each run re-reads a whole data source
and re-renders its Doc from scratch, so edits, deletions and reorders made in
Notion all show up correctly and nothing can drift out of sync. The stored hash
exists only so an unchanged run skips the Drive write — otherwise NotebookLM
would re-index a Doc whose content did not move.
"""

import hashlib
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import buffer
import drive
import notion_read

LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Kolkata")

# Per-type presentation. LABEL prefixes every session heading so a NotebookLM
# answer citing one source still says which kind of session it came from, and
# the "companion" sentence in _render names the other Doc so questions spanning
# both sources have something to hang on to.
_DOC_TITLES = {
    "class": "Ballet — Class Notes",
    "floor": "Ballet — Floor Barre Notes",
}
_LABELS = {"class": "Class", "floor": "Floor barre"}
_BLURBS = {
    "class": "studio class sessions",
    "floor": "Kniaseff floor barre sessions",
}

logger = logging.getLogger()


def _format_session(session_type: str, entry: dict, body: str) -> str:
    label = _LABELS[session_type]
    date = entry["date"]
    if date:
        heading = f"## {label} — {date}"
    else:
        # Hand-created rows can be missing Date. Still export the session rather
        # than dropping it — fall back to the row title and flag the gap.
        logger.warning("Entry %s has no Date property", entry["id"])
        heading = f"## {label} — {entry['title'] or '(untitled)'}"

    lines = [heading]
    if entry["exercises"]:
        lines.append(f"**Exercises completed:** {', '.join(entry['exercises'])}")
    lines.append("")
    lines.append(body.strip())
    return "\n".join(lines)


def _render_sessions(session_type: str, entries: list, bodies: list) -> str:
    """Join every session's rendered block. This is the part that gets hashed
    for change detection, so it must contain nothing that varies run-to-run when
    the underlying Notion data has not changed — no "last synced" clock.
    """
    sessions = [
        _format_session(session_type, entry, body)
        for entry, body in zip(entries, bodies)
    ]
    return "\n\n---\n\n".join(sessions)


def _render(session_type: str, entries: list, sessions_text: str) -> str:
    now = datetime.now(ZoneInfo(LOCAL_TZ))
    dates = [e["date"] for e in entries if e["date"]]
    date_range = f"{min(dates)} → {max(dates)}" if dates else "no dates recorded"
    other = "floor" if session_type == "class" else "class"

    # "Last synced" is deliberately excluded from the hashed content (see
    # _render_sessions) — it changes every run regardless of whether the Notion
    # data did, which would defeat the hash gate entirely.
    header = (
        f"# {_DOC_TITLES[session_type]}\n\n"
        "Auto-generated from Notion. Do not edit here — edits are overwritten on "
        "the next sync.\n"
        f"Last synced: {now.strftime('%Y-%m-%d %H:%M')} {LOCAL_TZ.split('/')[-1]} · "
        f"{len(entries)} sessions · {date_range}\n\n"
        f"This document holds {_BLURBS[session_type]} only. Its companion source "
        f'in the same notebook, "{_DOC_TITLES[other]}", holds '
        f"{_BLURBS[other]}.\n\n"
        f'Each "##" heading below is one session, oldest first. Body is the '
        "condensed post-session journal entry.\n"
    )

    return header + "\n---\n\n" + sessions_text + "\n"


def sync_one(session_type: str) -> dict:
    """Rebuild one Doc. Returns {"type", "changed", "sessions"}."""
    entries = notion_read.list_entries(session_type)
    bodies = [notion_read.page_markdown(entry["id"]) for entry in entries]
    sessions_text = _render_sessions(session_type, entries, bodies)

    content_hash = hashlib.sha256(sessions_text.encode("utf-8")).hexdigest()
    if content_hash == buffer.get_sync_hash(session_type):
        logger.info(
            "No change since last %s sync (%d sessions), skipping Drive write",
            session_type,
            len(entries),
        )
        return {"type": session_type, "changed": False, "sessions": len(entries)}

    # Drive write before hash write: if the Drive call fails, the stored hash
    # stays at its old value and the next run retries. Writing the hash first
    # would mark this run as done even on a failed upload, and the Doc would
    # silently drift out of sync forever.
    document = _render(session_type, entries, sessions_text)
    drive.replace_doc(session_type, document)
    buffer.put_sync_hash(session_type, content_hash)

    logger.info(
        "Synced %d %s sessions to Doc %s",
        len(entries),
        session_type,
        drive.DRIVE_DOC_IDS[session_type],
    )
    return {"type": session_type, "changed": True, "sessions": len(entries)}


def sync_all() -> list:
    """Rebuild both Docs. Returns one result dict per session type."""
    return [sync_one(session_type) for session_type in ("class", "floor")]
