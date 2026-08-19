"""Read side of the Notion integration — the sync's source of truth.

notion.py writes pages; this reads them back. Split into its own module so the
write path (which every /done depends on) stays free of the rate-limit pacing
and pagination this needs.
"""

import logging
import os
import time

import requests

from notion import API_BASE, DATA_SOURCE_IDS, HEADERS

# Notion's own property name on the floor barre data source. Set this env var if
# it ever gets renamed in Notion — no code change needed. The class data source
# has no such property; extraction returns [] there.
EXERCISES_PROPERTY = os.environ.get("NOTION_EXERCISES_PROPERTY", "Exercises completed")

logger = logging.getLogger()

# Notion's documented average rate limit is ~3 requests/second. A sync makes one
# query call plus one markdown call per session across both databases, so it is
# the only caller that can burst against that ceiling.
_RATE_DELAY_SECONDS = 0.34
_MAX_RETRIES = 3


def _request(method: str, path: str, **kwargs) -> requests.Response:
    """Call the Notion API with rate-limit pacing and retry on 429/5xx."""
    for attempt in range(_MAX_RETRIES):
        time.sleep(_RATE_DELAY_SECONDS)
        resp = requests.request(
            method, f"{API_BASE}{path}", headers=HEADERS, timeout=20, **kwargs
        )
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "1"))
            logger.warning("Notion rate limited us, sleeping %.1fs", retry_after)
            time.sleep(retry_after)
            continue
        if resp.status_code >= 500:
            logger.warning("Notion API %s%s -> %s, retrying", method, path, resp.status_code)
            time.sleep(2**attempt)
            continue
        return resp
    return resp  # last attempt's response, even if still failing; caller checks .ok


def _extract_title(properties: dict) -> str:
    title_parts = properties.get("Name", {}).get("title") or []
    return "".join(part.get("plain_text", "") for part in title_parts).strip()


def _extract_date(properties: dict) -> str | None:
    date_obj = properties.get("Date", {}).get("date")
    return date_obj.get("start") if date_obj else None


def _extract_exercises(properties: dict) -> list:
    options = properties.get(EXERCISES_PROPERTY, {}).get("multi_select") or []
    return [opt.get("name", "") for opt in options if opt.get("name")]


def list_entries(session_type: str) -> list:
    """Return every row in one data source, oldest session first.

    Each item: {"id", "title", "date" (may be None), "exercises" (may be [])}.
    Extraction tolerates missing/null fields — hand-edited Notion rows can have
    gaps, and one malformed row must never abort the whole sync.
    """
    entries = []
    start_cursor = None
    while True:
        body = {
            "sorts": [{"property": "Date", "direction": "ascending"}],
            "page_size": 100,
        }
        if start_cursor:
            body["start_cursor"] = start_cursor

        resp = _request(
            "POST", f"/data_sources/{DATA_SOURCE_IDS[session_type]}/query", json=body
        )
        if not resp.ok:
            logger.error("Notion query error %s: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()

        for page in data.get("results", []):
            properties = page.get("properties", {})
            entries.append(
                {
                    "id": page["id"],
                    "title": _extract_title(properties),
                    "date": _extract_date(properties),
                    "exercises": _extract_exercises(properties),
                }
            )

        if not data.get("has_more"):
            break
        start_cursor = data.get("next_cursor")

    return entries


def page_markdown(page_id: str) -> str:
    """Fetch a page's body content rendered as markdown."""
    resp = _request("GET", f"/pages/{page_id}/markdown")
    if not resp.ok:
        logger.error("Notion markdown error %s: %s", resp.status_code, resp.text)
    resp.raise_for_status()
    data = resp.json()
    if data.get("truncated"):
        logger.warning("Page %s markdown was truncated by Notion", page_id)
    return data.get("markdown", "")
