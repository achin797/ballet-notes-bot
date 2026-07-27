import logging

import functions_framework

import buffer
import condense
import notion
import telegram

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_HELP_TEXT = (
    "Log a session in three steps:\n\n"
    "1. /class or /floor — tell me which kind of session it was\n"
    "2. send your raw notes, across as many messages as you like\n"
    "3. /done — condense them and add the entry to Notion\n\n"
    "Other commands:\n"
    "/start, /help — show this message\n"
    "/quit — discard the current session and start over"
)

_NO_SESSION_TEXT = (
    "Which session was this? Send /class or /floor first, then your notes."
)

# Telegram only retries when it doesn't get a 200, so every path returns one —
# including the paths where we deliberately ignore the request.
_OK = ("", 200)


@functions_framework.http
def main(request):
    # Verify the shared secret first: the function is deployed unauthenticated,
    # so unsigned requests are the internet knocking, not Telegram.
    if not telegram.verify_secret(request.headers):
        logger.warning("Rejected request with missing/invalid webhook secret")
        return _OK

    update = request.get_json(silent=True) or {}

    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")
    update_id = update.get("update_id")

    if chat_id is None or text is None:
        # Non-text update (photo, sticker, edited_message, etc.) — nothing to do.
        return _OK

    if not telegram.is_allowed_chat(chat_id):
        telegram.send_message(chat_id, "Not authorized.")
        return _OK

    if update_id is not None and buffer.is_duplicate_update(update_id):
        logger.info("Skipping already-processed update_id=%s", update_id)
        return _OK

    stripped = text.strip()

    if stripped in ("/start", "/help"):
        telegram.send_message(chat_id, _HELP_TEXT)
        return _OK

    if stripped in ("/class", "/floor"):
        session_type = stripped.lstrip("/")
        buffer.set_session_type(chat_id, session_type)
        label = "Class" if session_type == "class" else "Floor barre"
        telegram.send_message(
            chat_id, f"{label} session started. Send your notes, then /done."
        )
        return _OK

    if stripped == "/quit":
        chunks, _ = buffer.get_and_clear_buffer(chat_id)
        if chunks:
            telegram.send_message(chat_id, "Discarded. Start again with /class or /floor.")
        else:
            telegram.send_message(chat_id, "Nothing buffered — already a clean slate.")
        return _OK

    if stripped == "/done":
        chunks, session_type = buffer.get_and_clear_buffer(chat_id)
        if not session_type:
            telegram.send_message(chat_id, _NO_SESSION_TEXT)
            return _OK
        if not chunks:
            telegram.send_message(chat_id, "No notes yet — send them first, then /done.")
            return _OK

        raw_notes = "\n".join(chunks)
        try:
            condensed = condense.condense(raw_notes, session_type)
            url = notion.create_entry(
                session_type=session_type, condensed=condensed, raw_notes=raw_notes
            )
            telegram.send_message(chat_id, f"✅ Added to Notion\n{url}")
        except Exception:
            logger.exception("Failed to condense/write notes for chat_id=%s", chat_id)
            telegram.send_message(
                chat_id,
                "⚠️ Something went wrong condensing/saving those notes. "
                "Your raw notes were not saved — please resend them.",
            )
        return _OK

    # Any other text is a note. Raw notes usually arrive as several messages,
    # since Telegram caps a single message at 4096 characters.
    count = buffer.append_chunk(chat_id, text)
    if count is None:
        telegram.send_message(chat_id, _NO_SESSION_TEXT)
        return _OK
    telegram.send_message(
        chat_id, f"Got it ({count} message(s) buffered) — send /done when finished."
    )
    return _OK
