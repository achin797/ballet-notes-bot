import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
# 2026-03-11 or later is required for the page-content "markdown" field.
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2026-03-11")
LOCAL_TZ = os.environ.get("LOCAL_TZ", "Asia/Kolkata")

DATA_SOURCE_IDS = {
    "class": os.environ["NOTION_CLASS_DATA_SOURCE_ID"],
    "floor": os.environ["NOTION_FLOOR_DATA_SOURCE_ID"],
}

API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# Notion caps a single rich_text object's text.content at 2000 chars.
_CHUNK_SIZE = 2000


def chunk(text: str) -> list:
    """Split text into a list of Notion rich_text objects, each <= 2000 chars."""
    if not text:
        return [{"text": {"content": ""}}]
    return [
        {"text": {"content": text[i : i + _CHUNK_SIZE]}}
        for i in range(0, len(text), _CHUNK_SIZE)
    ]


def create_entry(session_type: str, condensed: str, raw_notes: str) -> str:
    """Create a page in the Class or Floor barre database. Returns its URL.

    Both databases share the same three properties (Name, Date, Raw notes), so
    session_type only selects which one to write to. Floor barre's extra
    "Exercises completed" multi-select is left untouched — deciding which
    exercises a session covered is condensing work, not plumbing.
    """
    now = datetime.now(ZoneInfo(LOCAL_TZ))
    # Title is the month name followed by the day number, e.g. "July 27".
    title = f"{now.strftime('%B')} {now.day}"

    payload = {
        "parent": {
            "type": "data_source_id",
            "data_source_id": DATA_SOURCE_IDS[session_type],
        },
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": now.strftime("%Y-%m-%d")}},
            "Raw notes": {"rich_text": chunk(raw_notes)},
        },
        # Notion parses this server-side into real blocks; markdown must not be
        # pre-chunked, since splitting mid-token would corrupt the syntax.
        "markdown": condensed,
    }

    resp = requests.post(f"{API_BASE}/pages", headers=HEADERS, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()["url"]
