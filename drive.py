"""Write the rendered markdown into the target Google Docs.

Auth note — the reason this is not just google.auth.default(scopes=[...]):
Cloud Functions gen2 runs on Cloud Run, whose metadata server issues tokens
scoped to cloud-platform only. cloud-platform covers Google Cloud APIs; Drive is
a Workspace API and is not among them, so a metadata token gets a 403
"insufficient authentication scopes" from Drive no matter how the file is
shared. Impersonating our own service account through the IAM Credentials API
mints a fresh token with the Drive scope explicitly requested. Same identity,
wider scope, and no service-account key anywhere.
"""

import os

import google.auth
import requests
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request

DRIVE_DOC_IDS = {
    "class": os.environ["DRIVE_CLASS_DOC_ID"],
    "floor": os.environ["DRIVE_FLOOR_DOC_ID"],
}

# The function's own runtime service account. Set explicitly rather than read
# from the metadata server so a misconfigured deploy fails at import with a
# named variable instead of at the first /sync with a 403.
_IMPERSONATE_SA = os.environ["DRIVE_IMPERSONATE_SA"]

# Full drive scope, not the narrower drive.file: drive.file only covers files
# the calling app itself created, but these Docs are created and owned by the
# human user and merely shared with the service account. The SA's Drive access
# is bounded by those two shares regardless of which scope is requested, so
# drive.file would just 404 on files it did not create.
_SCOPES = ["https://www.googleapis.com/auth/drive"]
_DOC_MIME = "application/vnd.google-apps.document"
# Drive converts markdown media into native Doc formatting (real H1/H2, bold,
# horizontal rules). Fallback ladder if this ever stops working: "text/html"
# (render markdown -> minimal HTML first), then "text/plain" as a last resort
# (content stays searchable, just unstyled).
_MEDIA_MIME = "text/markdown"
_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=multipart"

# Built once at cold start; the constructor makes no network call. The token is
# fetched lazily and refreshed per call below rather than cached, since a warm
# instance can outlive a token.
_source_credentials, _ = google.auth.default()
_credentials = impersonated_credentials.Credentials(
    source_credentials=_source_credentials,
    target_principal=_IMPERSONATE_SA,
    target_scopes=_SCOPES,
    lifetime=3600,
)


def _token() -> str:
    _credentials.refresh(Request())
    return _credentials.token


def replace_doc(session_type: str, markdown: str) -> None:
    """Overwrite one target Google Doc's full content from markdown.

    Drive's media-upload conversion replaces the entire document body on update
    — there is no append or patch-in-place. That is exactly what is wanted here:
    the caller always sends a freshly regenerated full document, so a full
    overwrite is the idempotent behavior (see drive_sync.py).
    """
    doc_id = DRIVE_DOC_IDS[session_type]
    boundary = "ballet_notes_drive_sync_boundary"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        f'{{"mimeType": "{_DOC_MIME}"}}\r\n'
        f"--{boundary}\r\n"
        f"Content-Type: {_MEDIA_MIME}; charset=UTF-8\r\n\r\n"
        f"{markdown}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    # Built by hand rather than with requests' files=: that produces
    # multipart/form-data, which Drive's upload endpoint rejects. Drive requires
    # multipart/related with this exact two-part shape.
    resp = requests.patch(
        _UPLOAD_URL.format(file_id=doc_id),
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        data=body,
        timeout=60,
    )
    resp.raise_for_status()
