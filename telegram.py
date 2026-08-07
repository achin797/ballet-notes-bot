import os

import requests

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


def send_message(chat_id, text: str) -> None:
    # Telegram caps sendMessage text at 4096 chars; truncate defensively so a
    # reply never fails outright (the Notion page itself holds the full content).
    if len(text) > 4000:
        text = text[:3990] + "\n…(truncated)"
    resp = requests.post(
        f"{API_BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )
    resp.raise_for_status()
