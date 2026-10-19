import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import functions_framework

import buffer
import condense
import drive_sync
import notion
import telegram

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_HELP_TEXT = (
    "Log a session in three steps:\n\n"
    "1. /class, /floor, /explore or /skill — tell me which kind of session it was\n"
    "2. send your raw notes, across as many messages as you like\n"
    "3. /done — condense them and add the entry to Notion\n\n"
    "With topics on, step 1 opens a new topic for the session. Send its notes and "
    "its /done inside that topic — several sessions can be open at once.\n\n"
    "Other commands:\n"
    "/start, /help — show this message\n"
    "/sessions — list the open sessions\n"
    "/quit — discard the current session's notes\n"
    "/sync — push all Notion databases to their Google Docs (NotebookLM)"
)

_NO_SESSION_TEXT = (
    "Which session was this? Send /class, /floor, /explore or /skill first, then your notes."
)

_SESSION_COMMANDS = {f"/{t}": t for t in buffer.SESSION_TYPES}

# Telegram only retries when it doesn't get a 200, so every path returns one —
# including the paths where we deliberately ignore the request.
_OK = ("", 200)


def _audio_part(message):
    """Pull the audio out of a message, whichever field Telegram put it in.

    Three fields can carry it, and they are not interchangeable:
      voice     — recorded in Telegram, ogg/opus
      audio     — an audio *file*, which is what a voice note forwarded from
                  WhatsApp arrives as (m4a)
      document  — anything sent as a file; only treated as audio if it says so

    Returns None for everything else, including video_note.
    """
    part = message.get("voice") or message.get("audio")
    if part is not None:
        return part
    doc = message.get("document") or {}
    if (doc.get("mime_type") or "").startswith("audio/"):
        return doc
    return None


def _command(text: str):
    """The command a message is, or None if it's a note.

    Only a message that is nothing but the command counts ("/floor stretched"
    is a note), and the "@botname" suffix Telegram adds in some clients is dropped.
    """
    parts = text.strip().split()
    if len(parts) != 1 or not parts[0].startswith("/"):
        return None
    return parts[0].split("@", 1)[0].lower()


def _topic_name(session_type: str, started_at: datetime) -> str:
    local = started_at.astimezone(ZoneInfo(notion.LOCAL_TZ))
    return (
        f"{drive_sync.LABELS[session_type]} · "
        f"{local.strftime('%b')} {local.day}, {local.strftime('%H:%M')}"
    )


def _session_name(session) -> str:
    return session.get("topic_name") or _topic_name(
        session["session_type"], session.get("started_at") or datetime.now(timezone.utc)
    )


def _open_sessions_text(chat_id) -> str:
    sessions = buffer.list_open(chat_id)
    if not sessions:
        return "No open sessions. Start one with /class, /floor, /explore or /skill."
    lines = ["Open sessions:"]
    for session in sessions:
        count = len(session.get("chunks") or [])
        saving = " — saving…" if session.get("status") == buffer.FINISHING else ""
        lines.append(f"• {_session_name(session)} ({count} note(s)){saving}")
    return "\n".join(lines)


class _Where:
    """Which chat and topic a message came from, and how to answer it.

    Three modes:
      topics off            thread_id None, in_general False — one session per chat,
                            exactly as before topics existed
      topics on, General    thread_id None, in_general True — no session lives here
      topics on, in a topic thread_id set — that topic's session
    """

    def __init__(self, chat_id, thread_id, in_general):
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.in_general = in_general

    @property
    def in_topic(self) -> bool:
        return self.thread_id is not None

    def reply(self, text: str) -> None:
        telegram.send_message(self.chat_id, text, self.thread_id)

    def refuse(self, outcome: str) -> None:
        """Explain why a note or voice note in this place wasn't saved."""
        if outcome == "finishing":
            self.reply("⏳ This session is being saved to Notion — that message wasn't added.")
        elif self.in_topic and outcome == "closed":
            self.reply("This session is closed. Start a new one from General with /class, /floor, /explore or /skill.")
        elif self.in_topic:
            self.reply(
                "No open session in this topic — it may have expired after 48 hours of "
                "silence. Start one from General with /class, /floor, /explore or /skill, "
                "or send one of those here to use this topic."
            )
        else:
            self.reply(_NO_SESSION_TEXT)

    def point_to_topics(self, what: str) -> None:
        self.reply(f"{what} inside a session's topic, not in General.\n\n" + _open_sessions_text(self.chat_id))


def _start(where: _Where, session_type: str) -> None:
    label = drive_sync.LABELS[session_type]

    if where.in_general:
        name = _topic_name(session_type, datetime.now(timezone.utc))
        try:
            thread_id = telegram.create_topic(where.chat_id, name)
        except Exception:
            logger.exception("createForumTopic failed")
            where.reply(
                f"⚠️ Couldn't open a topic. Create one yourself and send "
                f"/{session_type} inside it."
            )
            return
        buffer.start_session(where.chat_id, thread_id, session_type, name)
        telegram.send_message(
            where.chat_id, f"{label} session started. Send your notes here, then /done.", thread_id
        )
        where.reply(f"Opened topic “{name}”.")
        return

    outcome, session = buffer.start_session(where.chat_id, where.thread_id, session_type)
    if outcome == "started":
        where.reply(f"{label} session started. Send your notes, then /done.")
    elif outcome == "same":
        where.reply(f"{label} session already open — keep sending notes, then /done.")
    elif outcome == "conflict":
        other = drive_sync.LABELS[session["session_type"]]
        if where.in_topic:
            where.reply(
                f"This topic is a {other} session. Start a {label} session from General."
            )
        else:
            where.reply(f"A {other} session is open. Send /done or /quit first.")
    else:  # closed
        where.reply(
            "This topic's session is closed. Start a new one from General with "
            f"/{session_type}."
        )


def _done(where: _Where) -> None:
    if where.in_general:
        where.point_to_topics("Send /done")
        return

    outcome, session = buffer.begin_finish(where.chat_id, where.thread_id)
    if outcome == "finishing":
        where.reply("⏳ Already saving this session — hang on.")
        return
    if outcome == "empty":
        where.reply("No notes yet — send them first, then /done.")
        return
    if outcome != "ok":
        if where.in_topic and outcome == "closed":
            where.reply("This session is already closed.")
        else:
            where.refuse(outcome)
        return

    session_type = session["session_type"]
    raw_notes = "\n".join(session["texts"])
    try:
        condensed = condense.condense(raw_notes, session_type)
        url = notion.create_entry(
            session_type=session_type,
            condensed=condensed,
            raw_notes=raw_notes,
            started_at=session.get("started_at"),
        )
    except Exception:
        logger.exception("Failed to condense/write notes for chat_id=%s", where.chat_id)
        try:
            buffer.abort_finish(where.chat_id, where.thread_id)
        except Exception:
            # Left "finishing", it unlocks itself after buffer.STALE_FINISH_SECONDS.
            logger.exception("Failed to reopen session after a failed /done")
        where.reply(
            "⚠️ Something went wrong condensing/saving those notes. They're still "
            "kept — send /done again to retry."
        )
        return

    try:
        buffer.complete_finish(where.chat_id, where.thread_id)
    except Exception:
        # The page exists; the worst case is the session reading "saving…" until
        # it goes stale, so report success regardless.
        logger.exception("Failed to mark session done after writing %s", url)
    where.reply(f"✅ Added to Notion\n{url}")
    if where.in_topic:
        telegram.rename_topic(where.chat_id, where.thread_id, "✅ " + _session_name(session))


def _quit(where: _Where) -> None:
    if where.in_general:
        where.point_to_topics("Send /quit")
        return

    outcome, session = buffer.discard(where.chat_id, where.thread_id)
    if outcome == "ok":
        if where.in_topic:
            where.reply("Discarded. The topic stays as a record; start a new session from General.")
            telegram.rename_topic(where.chat_id, where.thread_id, "🗑 " + _session_name(session))
        else:
            where.reply("Discarded. Start again with /class, /floor, /explore or /skill.")
    elif outcome == "finishing":
        where.reply("⏳ This session is being saved to Notion — too late to discard it.")
    else:
        where.reply("Nothing to discard here.")


def _sync(where: _Where) -> None:
    # Acknowledged before the work starts: a full rebuild reads every page in
    # every database, which takes long enough that a silent bot looks broken.
    where.reply("Syncing Notion → Google Docs…")
    try:
        results = drive_sync.sync_all()
    except Exception:
        logger.exception("Drive sync failed for chat_id=%s", where.chat_id)
        where.reply(
            "⚠️ Sync failed. The Docs were not changed, so NotebookLM still "
            "has the last good version. Try again in a moment."
        )
        return
    lines = []
    for result in results:
        label = drive_sync.LABELS[result["type"]]
        state = "updated" if result["changed"] else "already up to date"
        lines.append(f"{label}: {state} ({result['sessions']} sessions)")
    where.reply("✅ Sync done\n" + "\n".join(lines))


def _voice(where: _Where, voice, message_id) -> None:
    if where.in_general:
        where.point_to_topics("Send voice notes")
        return
    # Checked before transcribing rather than relying on append_chunk's refusal,
    # so a note with nowhere to go doesn't cost a model call.
    session = buffer.get_session(where.chat_id, where.thread_id)
    if session is None or session.get("status") != buffer.OPEN:
        where.refuse(
            "no_session" if session is None
            else "finishing" if session.get("status") == buffer.FINISHING
            else "closed"
        )
        return
    try:
        audio = telegram.download_file(voice["file_id"])
        transcript = condense.transcribe(audio, voice.get("mime_type") or "audio/ogg")
    except Exception:
        logger.exception("Failed to transcribe voice note for chat_id=%s", where.chat_id)
        where.reply(
            "⚠️ Couldn't transcribe that voice note. It wasn't saved — "
            "please send it again, or type the notes instead."
        )
        return
    result = buffer.append_chunk(where.chat_id, where.thread_id, transcript, message_id)
    if isinstance(result, str):
        # Session ended (a /done or /quit landed) while we were transcribing.
        where.refuse(result)
        return
    where.reply(f"🎙 Transcribed ({result} message(s) buffered) — send /done when finished.")


def _note(where: _Where, text: str, message_id) -> None:
    if where.in_general:
        where.point_to_topics("Send notes")
        return
    # Raw notes usually arrive as several messages, since Telegram caps a
    # single message at 4096 characters.
    result = buffer.append_chunk(where.chat_id, where.thread_id, text, message_id)
    if isinstance(result, str):
        where.refuse(result)
        return
    where.reply(f"Got it ({result} message(s) buffered) — send /done when finished.")


@functions_framework.http
def main(request):
    # Verify the shared secret first: the function is deployed unauthenticated,
    # so unsigned requests are the internet knocking, not Telegram.
    if not telegram.verify_secret(request.headers):
        logger.warning("Rejected request with missing/invalid webhook secret")
        return _OK

    update = request.get_json(silent=True) or {}

    edited = "message" not in update and "edited_message" in update
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")
    voice = _audio_part(message)
    message_id = message.get("message_id")
    update_id = update.get("update_id")

    if chat_id is None or (text is None and voice is None):
        # Neither text nor audio (photo, sticker, topic-created service message,
        # etc.). Logged rather than dropped quietly: an unhandled message type is
        # otherwise indistinguishable from the bot being down.
        logger.info("Ignoring update with no text or audio: fields=%s", sorted(message))
        return _OK

    if not telegram.is_allowed_chat(chat_id):
        telegram.send_message(chat_id, "Not authorized.")
        return _OK

    if update_id is not None and buffer.is_duplicate_update(update_id):
        logger.info("Skipping already-processed update_id=%s", update_id)
        return _OK

    topics = telegram.topics_enabled()
    # is_topic_message, not just message_thread_id: a plain reply can carry a
    # thread id too, and must not split off into a session of its own.
    thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None
    if not topics:
        thread_id = None
    logger.info(
        "update_id=%s topics=%s message_thread_id=%s is_topic_message=%s",
        update_id, topics, message.get("message_thread_id"), message.get("is_topic_message"),
    )
    where = _Where(chat_id, thread_id, in_general=topics and thread_id is None)

    if edited:
        # Only a still-open session's notes can change; edited commands and
        # voice notes are ignored, since re-running a command on edit would surprise.
        if text is not None and _command(text) is None and not where.in_general:
            if buffer.replace_chunk(chat_id, thread_id, message_id, text):
                where.reply("✏️ Updated that note.")
        return _OK

    if voice is not None:
        _voice(where, voice, message_id)
        return _OK

    command = _command(text)

    if command in ("/start", "/help"):
        where.reply(_HELP_TEXT)
    elif command in _SESSION_COMMANDS:
        _start(where, _SESSION_COMMANDS[command])
    elif command == "/sessions":
        where.reply(_open_sessions_text(chat_id))
    elif command == "/done":
        _done(where)
    elif command == "/quit":
        _quit(where)
    elif command == "/sync":
        _sync(where)
    else:
        # Unknown "/word" commands fall through as notes, as before.
        _note(where, text, message_id)
    return _OK
