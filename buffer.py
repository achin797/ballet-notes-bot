import os
from datetime import datetime, timedelta, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "buffers")

# One collection holds three document kinds, distinguished by id prefix:
#   "sess_<chat_id>_<thread>" - one session: raw-note chunks, type, status.
#                               <thread> is the Telegram topic id, or "main"
#                               when the bot's threaded mode is off.
#   "upd_<update_id>"         - a marker for an already-processed Telegram update
#   "sync_<type>"             - hash of the last Google Doc written for class | floor | explore | skill
# A single collection keeps the Firestore TTL policy to one field on one path.
SESSION_TTL_SECONDS = 48 * 60 * 60  # an open session survives 48 hours of silence
CLOSED_TTL_SECONDS = 30 * 24 * 60 * 60  # done/discarded markers: refuse reuse of old topics
DEDUP_TTL_SECONDS = 60 * 60  # dedup markers only need to outlive Telegram's retries
# A /done that has been "finishing" longer than the function's 540s timeout
# must have crashed mid-way, so a new /done may take it over.
STALE_FINISH_SECONDS = 10 * 60

SESSION_TYPES = ("class", "floor", "explore", "skill")

OPEN, FINISHING, DONE, DISCARDED = "open", "finishing", "done", "discarded"

_db = firestore.Client()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expiry(seconds: int) -> datetime:
    # Firestore TTL policies require a real timestamp field, not an epoch int.
    return _now() + timedelta(seconds=seconds)


def _session_ref(chat_id, thread_id):
    return _db.collection(COLLECTION).document(
        f"sess_{chat_id}_{thread_id if thread_id is not None else 'main'}"
    )


def _live(snapshot):
    """The session dict, or None if missing or past its expiry.

    Firestore's TTL deletion runs up to ~24h late (and may not be enabled at
    all), so an expired document still has to read as gone.
    """
    if not snapshot.exists:
        return None
    data = snapshot.to_dict()
    expire_at = data.get("expireAt")
    if expire_at is not None and expire_at < _now():
        return None
    return data


def get_session(chat_id, thread_id):
    """The session in this topic (any status), or None."""
    return _live(_session_ref(chat_id, thread_id).get())


@firestore.transactional
def _start(transaction, ref, fields, reuse_closed):
    data = _live(ref.get(transaction=transaction))
    if data is not None:
        status = data.get("status")
        if status in (OPEN, FINISHING):
            if data.get("session_type") == fields["session_type"]:
                return "same", data
            return "conflict", data
        if not reuse_closed:
            return "closed", data
    transaction.set(ref, fields)
    return "started", fields


def start_session(chat_id, thread_id, session_type: str, topic_name=None):
    """Open a session in this topic. Returns (outcome, session):

      "started"   new session created
      "same"      a session of this type is already open here — nothing changed
      "conflict"  a session of another type is open here — nothing changed
      "closed"    this topic's session was finished or discarded — topics are
                  single-use, so nothing changed

    Without topics (thread_id None) there is only one slot per chat, so a closed
    session there is simply replaced.
    """
    if session_type not in SESSION_TYPES:
        raise ValueError(f"unknown session_type: {session_type!r}")
    fields = {
        "session_type": session_type,
        "chat_id": chat_id,
        "thread_id": thread_id,
        "topic_name": topic_name,
        "status": OPEN,
        "started_at": _now(),
        "chunks": [],
        "expireAt": _expiry(SESSION_TTL_SECONDS),
    }
    return _start(
        _db.transaction(), _session_ref(chat_id, thread_id), fields, thread_id is None
    )


@firestore.transactional
def _append(transaction, ref, text, message_id):
    data = _live(ref.get(transaction=transaction))
    if data is None:
        return "no_session"
    if data.get("status") == FINISHING:
        # Saving it now would get it wiped when the running /done completes.
        return "finishing"
    if data.get("status") != OPEN:
        return "closed"
    # Read-modify-write rather than ArrayUnion: ArrayUnion dedupes equal values,
    # which would silently drop a note the user genuinely sent twice.
    chunks = list(data.get("chunks", []))
    chunks.append({"text": text, "message_id": message_id})
    transaction.update(
        ref, {"chunks": chunks, "expireAt": _expiry(SESSION_TTL_SECONDS)}
    )
    return len(chunks)


def append_chunk(chat_id, thread_id, text: str, message_id):
    """Add a raw-notes message. Returns the new chunk count (int), or one of
    "no_session", "finishing", "closed"."""
    return _append(_db.transaction(), _session_ref(chat_id, thread_id), text, message_id)


@firestore.transactional
def _replace(transaction, ref, message_id, text):
    data = _live(ref.get(transaction=transaction))
    if data is None or data.get("status") != OPEN:
        return False
    chunks = list(data.get("chunks", []))
    for chunk in chunks:
        if chunk.get("message_id") == message_id:
            chunk["text"] = text
            transaction.update(ref, {"chunks": chunks})
            return True
    return False


def replace_chunk(chat_id, thread_id, message_id, text: str) -> bool:
    """Swap in the edited text of an already-buffered message. True if replaced."""
    return _replace(_db.transaction(), _session_ref(chat_id, thread_id), message_id, text)


def _ordered_texts(chunks):
    # message_id rises in send order within a chat, so sorting by it undoes
    # out-of-order arrival (a slow voice transcription landing after a later text).
    return [c["text"] for c in sorted(chunks, key=lambda c: c.get("message_id") or 0)]


@firestore.transactional
def _begin_finish(transaction, ref):
    data = _live(ref.get(transaction=transaction))
    if data is None:
        return "no_session", None
    status = data.get("status")
    if status == FINISHING:
        since = data.get("finishing_since")
        if since is not None and _now() - since < timedelta(seconds=STALE_FINISH_SECONDS):
            return "finishing", None
    elif status != OPEN:
        return "closed", None
    if not data.get("chunks"):
        return "empty", None
    transaction.update(ref, {"status": FINISHING, "finishing_since": _now()})
    return "ok", {
        "session_type": data["session_type"],
        "started_at": data.get("started_at"),
        "topic_name": data.get("topic_name"),
        "texts": _ordered_texts(data["chunks"]),
    }


def begin_finish(chat_id, thread_id):
    """Lock the session for /done. Returns (outcome, session):

      "ok"          locked; session has session_type, started_at, topic_name, texts
      "no_session"  nothing here
      "finishing"   another /done is already running
      "closed"      already done or discarded
      "empty"       no notes yet — left open
    """
    return _begin_finish(_db.transaction(), _session_ref(chat_id, thread_id))


def complete_finish(chat_id, thread_id) -> None:
    """The Notion page exists: drop the notes, keep a marker so the topic isn't reused."""
    _session_ref(chat_id, thread_id).update(
        {
            "status": DONE,
            "chunks": [],
            "finishing_since": firestore.DELETE_FIELD,
            "expireAt": _expiry(CLOSED_TTL_SECONDS),
        }
    )


def abort_finish(chat_id, thread_id) -> None:
    """/done failed: reopen the session with its notes intact so it can be retried."""
    _session_ref(chat_id, thread_id).update(
        {
            "status": OPEN,
            "finishing_since": firestore.DELETE_FIELD,
            "expireAt": _expiry(SESSION_TTL_SECONDS),
        }
    )


@firestore.transactional
def _discard(transaction, ref):
    data = _live(ref.get(transaction=transaction))
    if data is None:
        return "no_session", None
    if data.get("status") == FINISHING:
        return "finishing", data
    if data.get("status") != OPEN:
        return "closed", data
    transaction.update(
        ref,
        {"status": DISCARDED, "chunks": [], "expireAt": _expiry(CLOSED_TTL_SECONDS)},
    )
    return "ok", data


def discard(chat_id, thread_id):
    """Drop an open session's notes. Returns (outcome, session) with outcome one of
    "ok", "no_session", "finishing", "closed"."""
    return _discard(_db.transaction(), _session_ref(chat_id, thread_id))


def list_open(chat_id):
    """Open or finishing sessions in this chat, oldest first.

    Filters on chat_id only and checks the rest here: a single equality filter
    needs no composite index, and a chat has at most a few hundred session docs.
    """
    docs = (
        _db.collection(COLLECTION)
        .where(filter=FieldFilter("chat_id", "==", chat_id))
        .stream()
    )
    sessions = [
        data
        for data in (_live(doc) for doc in docs)
        if data is not None and data.get("status") in (OPEN, FINISHING)
    ]
    return sorted(sessions, key=lambda s: s.get("started_at") or _now())


@firestore.transactional
def _claim(transaction, ref):
    snapshot = ref.get(transaction=transaction)
    if snapshot.exists:
        return True
    transaction.set(ref, {"expireAt": _expiry(DEDUP_TTL_SECONDS)})
    return False


def is_duplicate_update(update_id) -> bool:
    """Atomically mark update_id as processed. True if it was already processed.

    Telegram retries a webhook it doesn't get a fast 200 from, which would
    otherwise create duplicate Notion pages on a slow LLM call.
    """
    ref = _db.collection(COLLECTION).document(f"upd_{update_id}")
    return _claim(_db.transaction(), ref)


def _sync_ref(session_type: str):
    return _db.collection(COLLECTION).document(f"sync_{session_type}")


def get_sync_hash(session_type: str):
    """Content hash from the last successful Drive sync of this type, or None."""
    snapshot = _sync_ref(session_type).get()
    return snapshot.to_dict().get("contentHash") if snapshot.exists else None


def put_sync_hash(session_type: str, content_hash: str) -> None:
    # No expireAt on these documents, unlike sess_/upd_: the collection's TTL
    # policy only reaps documents that carry that field, so omitting it makes
    # these persist indefinitely — which is the point, they are the sync's only
    # memory between runs.
    _sync_ref(session_type).set(
        {"contentHash": content_hash, "syncedAt": datetime.now(timezone.utc)}
    )
