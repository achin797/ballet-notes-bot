import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
ALLOWED_CHAT_ID = os.environ["ALLOWED_CHAT_ID"]

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def verify_secret(headers) -> bool:
    # Telegram sends this header on every webhook POST. The function is deployed
    # unauthenticated, so this check is the only thing standing between the
    # endpoint and the open internet — a mismatch means we ignore the request.
    incoming = headers.get("X-Telegram-Bot-Api-Secret-Token")
    return bool(incoming) and incoming == TELEGRAM_WEBHOOK_SECRET


def is_allowed_chat(chat_id) -> bool:
    return str(chat_id) == str(ALLOWED_CHAT_ID)


def download_file(file_id: str) -> bytes:
    """Fetch a file the user sent (a voice note) as raw bytes.

    Two hops, which is how the Bot API exposes files: getFile resolves the id to a
    path, then the path is fetched from a different host. Telegram caps a bot's
    download at 20MB, which a voice note never approaches.

    The download URL embeds the bot token — never log it.
    """
    resp = requests.post(
        f"{API_BASE}/getFile", json={"file_id": file_id}, timeout=30
    )
    resp.raise_for_status()
    file_path = resp.json()["result"]["file_path"]

    download = requests.get(
        f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}",
        timeout=30,
    )
    download.raise_for_status()
    return download.content


class TelegramError(Exception):
    def __init__(self, status, description: str):
        super().__init__(f"{status}: {description}")
        self.status = status
        self.description = description


def _call(method: str, payload: dict, timeout: int = 10) -> dict:
    """POST a Bot API method and return its result. Raises TelegramError on any failure.

    Network errors are re-raised without their message, which would include the
    request URL — and the URL embeds the bot token.
    """
    try:
        resp = requests.post(f"{API_BASE}/{method}", json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise TelegramError(None, f"{method}: {type(exc).__name__}") from None
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not resp.ok or not body.get("ok"):
        raise TelegramError(resp.status_code, f"{method}: {body.get('description', 'no description')}")
    return body["result"]


def send_message(chat_id, text: str, thread_id=None):
    """Send a reply, into a topic when thread_id is set. Returns the message, or
    None if it couldn't be sent.

    Never raises: a reply is a notification, and a failed one must not turn a
    finished save into a reported failure. If the topic is gone (deleted by
    hand), the reply falls back to the main chat rather than vanishing.
    """
    # Telegram caps sendMessage text at 4096 chars; truncate defensively so a
    # reply never fails outright (the Notion page itself holds the full content).
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    payload = {"chat_id": chat_id, "text": text}
    if thread_id is not None:
        payload["message_thread_id"] = thread_id
    try:
        return _call("sendMessage", payload)
    except TelegramError as exc:
        if thread_id is not None and exc.status == 400:
            logger.warning("Reply to thread %s failed (%s); sending to main chat", thread_id, exc)
            payload.pop("message_thread_id")
            try:
                return _call("sendMessage", payload)
            except Exception:
                logger.exception("Fallback reply failed")
                return None
        logger.exception("sendMessage failed")
        return None
    except Exception:
        logger.exception("sendMessage failed")
        return None


_TOPICS_CACHE_SECONDS = 10 * 60
_topics_cache = {"value": None, "at": 0.0}


def topics_enabled() -> bool:
    """Whether threaded mode is on for this bot (BotFather → Threads Settings).

    Cached per instance for 10 minutes: toggling it in BotFather takes effect
    here within that window. If getMe fails, the last known value is reused,
    or False on a cold start — the old single-session behavior.
    """
    now = time.monotonic()
    if _topics_cache["value"] is not None and now - _topics_cache["at"] < _TOPICS_CACHE_SECONDS:
        return _topics_cache["value"]
    try:
        me = _call("getMe", {})
        _topics_cache["value"] = bool(me.get("has_topics_enabled"))
        _topics_cache["at"] = now
    except Exception:
        logger.exception("getMe failed; assuming topics=%s", _topics_cache["value"])
    return bool(_topics_cache["value"])


def create_topic(chat_id, name: str) -> int:
    """Create a topic in the chat and return its message_thread_id. Raises on failure."""
    result = _call("createForumTopic", {"chat_id": chat_id, "name": name[:128]})
    return result["message_thread_id"]


def rename_topic(chat_id, thread_id, name: str) -> None:
    """Best-effort rename: a stale topic title is cosmetic, so failures only log."""
    try:
        _call(
            "editForumTopic",
            {"chat_id": chat_id, "message_thread_id": thread_id, "name": name[:128]},
        )
    except Exception:
        logger.exception("editForumTopic failed for thread %s", thread_id)
