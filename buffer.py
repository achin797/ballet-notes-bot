import os
from datetime import datetime, timedelta, timezone

from google.cloud import firestore

COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "buffers")

# One collection holds three document kinds, distinguished by id prefix:
#   "buf_<chat_id>"    - the in-progress session: raw-note chunks + session type
#   "upd_<update_id>"  - a marker for an already-processed Telegram update
#   "sync_<type>"      - hash of the last Google Doc written for class | floor
# A single collection keeps the Firestore TTL policy to one field on one path.
BUFFER_TTL_SECONDS = 6 * 60 * 60  # abandoned sessions self-clean after 6h
DEDUP_TTL_SECONDS = 60 * 60  # dedup markers only need to outlive Telegram's retries

SESSION_TYPES = ("class", "floor")

_db = firestore.Client()


def _expiry(seconds: int) -> datetime:
    # Firestore TTL policies require a real timestamp field, not an epoch int.
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _buffer_ref(chat_id):
    return _db.collection(COLLECTION).document(f"buf_{chat_id}")


def set_session_type(chat_id, session_type: str) -> None:
    """Start (or re-label) this chat's session. Buffered chunks are left alone."""
    if session_type not in SESSION_TYPES:
        raise ValueError(f"unknown session_type: {session_type!r}")
    _buffer_ref(chat_id).set(
        {"session_type": session_type, "expireAt": _expiry(BUFFER_TTL_SECONDS)},
        merge=True,
    )


def get_session_type(chat_id):
    """Read the session type without touching the buffer. Returns None if unset.

    Exists so a voice note can be refused before it costs a transcription call —
    append_chunk() would refuse it too, but only after the audio had been processed.
    """
    snapshot = _buffer_ref(chat_id).get()
    return snapshot.to_dict().get("session_type") if snapshot.exists else None


@firestore.transactional
def _append(transaction, ref, text: str):
    snapshot = ref.get(transaction=transaction)
    data = snapshot.to_dict() if snapshot.exists else {}
    if not data.get("session_type"):
        # No session started — refuse rather than guess which database this belongs in.
        return None
    # Read-modify-write rather than ArrayUnion: ArrayUnion dedupes equal values,
    # which would silently drop a note the user genuinely sent twice, and it makes
    # no ordering promise. Arrival order is the whole point of the buffer.
    chunks = list(data.get("chunks", []))
    chunks.append(text)
    transaction.set(
        ref, {"chunks": chunks, "expireAt": _expiry(BUFFER_TTL_SECONDS)}, merge=True
    )
    return len(chunks)


def append_chunk(chat_id, text: str):
    """Append a raw-notes message. Returns the new chunk count, or None if no
    session type has been set yet (i.e. the user skipped /class or /floor)."""
    return _append(_db.transaction(), _buffer_ref(chat_id), text)


@firestore.transactional
def _take(transaction, ref):
    snapshot = ref.get(transaction=transaction)
    data = snapshot.to_dict() if snapshot.exists else {}
    transaction.delete(ref)
    return list(data.get("chunks", [])), data.get("session_type")


def get_and_clear_buffer(chat_id):
    """Return (chunks in arrival order, session_type) and clear the session."""
    return _take(_db.transaction(), _buffer_ref(chat_id))


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
    # No expireAt on these documents, unlike buf_/upd_: the collection's TTL
    # policy only reaps documents that carry that field, so omitting it makes
    # these persist indefinitely — which is the point, they are the sync's only
    # memory between runs.
    _sync_ref(session_type).set(
        {"contentHash": content_hash, "syncedAt": datetime.now(timezone.utc)}
    )
