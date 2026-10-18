# Ballet notes bot

Send raw notes to a Telegram bot after a ballet class, a floor barre session or a
movement exploration session — typed, spoken as voice notes, or both in the same
session. Each session lives in its own Telegram topic, so several can be open at
once. On `/done` it condenses the notes and adds a page to the matching Notion
database: title (month and day the session started, e.g. "July 27"), that date,
the raw notes, and the condensed entry as the page body.

```
raw notes -> Telegram bot -> Cloud Function (condense + write to Notion) -> Notion page
```

## Architecture

```
You (Telegram)
      │  /class, /floor or /explore (opens a topic), raw notes in it — typed and/or voice — then /done
      ▼
Telegram Bot API
      │  webhook POST on every message
      ▼
Cloud Function, gen2  (single function — main.py orchestrates everything below)
      │
      ├─ transcribes voice notes on arrival ──────────▶ prompts/transcribe.txt + LLM
      ├─ buffers messages & dedupes retried webhooks ──▶ Firestore
      ├─ condenses the notes on /done ─────────────────▶ prompts/ + LLM
      └─ writes the finished entry ────────────────────▶ Notion API
                                                              │
                                                              ▼
                                            New page in Ballet class notes,
                                                     Floor barre notes
                                                     or Movement exploration
```

| Piece           | Role                                                             | File                           |
|-----------------|------------------------------------------------------------------|--------------------------------|
| Telegram bot    | your only interface — send notes, get a confirmation + link back | `telegram.py`                  |
| Transcriber     | turns a voice note into the text a typed message would have been  | `condense.transcribe()`        |
| Cloud Function  | receives the webhook, routes commands, orchestrates the pipeline | `main.py`                      |
| Firestore       | holds each open session's messages (one per topic) until `/done`; dedupes retries | `buffer.py` |
| Condenser       | runs the per-session-type prompt over the raw notes               | `condense.py`, `prompts/*.txt` |
| Notion API      | creates the page: Name, Date, Raw notes + condensed body          | `notion.py`                    |

### Code map

| File | What's in it | Change it to… |
|------|--------------|---------------|
| `main.py` | webhook entry point; works out *where* a message came from (General, a topic, or topics off) and routes each command (`_start`, `_done`, `_quit`, `_note`, `_voice`, `_sync`) | add a command, change a bot reply |
| `buffer.py` | every Firestore read/write: sessions, dedup markers, sync hashes; all session state changes are transactions | change how sessions are stored, expiry times |
| `telegram.py` | Bot API calls: `send_message` (never raises), `topics_enabled`, `create_topic`, `rename_topic`, `download_file`, secret + chat checks | call a new Telegram method |
| `condense.py` | Gemini calls: `condense()` and `transcribe()` | change the model or its settings |
| `prompts/*.txt` | one condensing prompt per session type, plus `transcribe.txt` and `vocab.txt` | change what a Notion entry looks like |
| `notion.py` | creates the Notion page (`create_entry`) | change page properties or title format |
| `notion_read.py`, `drive.py`, `drive_sync.py` | the `/sync` pipeline below | change the NotebookLM Docs |
| `deploy.sh` | the one deploy command, with all non-secret config | change project, region, IDs |

Adding a fourth session type means: a prompt file, an entry in `SESSION_TYPES`
(`buffer.py`), `LABELS` (`drive_sync.py`), the prompt dict in `condense.py`,
`DATA_SOURCE_IDS` (`notion.py`), and its env vars in `deploy.sh`.

**Firestore documents** (one collection, `buffers`, told apart by id prefix):

- `sess_<chat_id>_<topic id>` — one session. `<topic id>` is `main` when threaded
  mode is off. Fields: `session_type`, `chat_id`, `thread_id`, `topic_name`,
  `status` (`open` → `finishing` → `done`, or `open` → `discarded`), `started_at`,
  `finishing_since` (only while finishing), `chunks` (list of `{text, message_id}`),
  `expireAt`.
- `upd_<update_id>` — "already handled" marker for a Telegram update.
- `sync_<type>` — content hash of the last Google Doc written by `/sync`.

Everything runs on request — there's no server to keep up. Cloud Functions and
Firestore only cost anything while actually processing a message, which for this
use case (a few sessions a week) is effectively free.

### Notion → Drive → NotebookLM (second pipeline)

```
You (Telegram)
      │  /sync
      ▼
Cloud Function, gen2  (same function, same code — main.py routes to drive_sync.py)
      │
      ├─ query every row, oldest first, every DB ──▶ Notion API (data source query)
      ├─ fetch each page's condensed body ─────────▶ Notion API (GET .../markdown)
      ├─ render one markdown document per DB
      ├─ hash it, skip the Drive write if unchanged since last sync
      └─ overwrite the whole Doc's content ────────▶ Google Drive API (multipart update)
                                                            │
                                                            ▼
                                     3 Google Docs, same file IDs every run
                                                            │
                                                            ▼ (automatic — no click)
                                                     NotebookLM source
```

| Piece              | Role                                                              | File           |
|---------------------|--------------------------------------------------------------------|-----------------|
| `/sync` command      | triggers a full rebuild of every Doc, on demand                   | `main.py`       |
| Notion API (read)   | data source query + per-page markdown fetch                       | `notion_read.py` |
| Google Drive API    | overwrites each target Doc's full content when its content changed | `drive.py`      |
| Firestore           | stores the SHA-256 of the last Doc content written, per session type | `buffer.py` (`sync_class`, `sync_floor`, `sync_explore`) |
| NotebookLM          | auto-syncs each Doc once added as a source — no manual re-add ever | (Google's UI, one-time) |

**Why a full overwrite every run, not an incremental append**: the sync has no
per-page state beyond one content hash per Doc. Every run re-reads the entire
Notion database and re-renders the whole Doc from scratch, so edits, deletions,
and reorders in Notion all show up correctly — there is nothing that can drift
out of sync. The hash exists only so an unchanged run skips the Drive write (so
NotebookLM doesn't re-index the Doc for no reason), not to decide what content
to send.

**Why NotebookLM needs no maintenance after the one-time setup**: Google's
automatic Drive sync for NotebookLM refreshes native Google Docs/Sheets/Slides
sources inside a notebook whenever the underlying Drive file changes, with no
sync button and no setting to turn on. That's the whole reason the target is a
**Google Doc** and not a plain `.md`/`.txt`/PDF file in Drive — only native
Workspace files get this treatment.

**Why the Drive auth is impersonation, not plain ADC**: Cloud Functions gen2
runs on Cloud Run, whose metadata server issues tokens scoped to
`cloud-platform` only. That scope covers Google Cloud APIs; Drive is a Workspace
API and isn't among them, so a plain metadata token gets a 403 "insufficient
authentication scopes" from Drive no matter how the file is shared. `drive.py`
works around this by impersonating the function's own service account through
the IAM Credentials API, asking for the Drive scope explicitly on that call.
Same identity, wider scope, no key file. Requires
`roles/iam.serviceAccountTokenCreator` granted to the service account on
itself — see "NotebookLM sync setup" below.

**Why the content hash excludes the "Last synced" line**: `drive_sync.py`
hashes only the rendered sessions, not the full document. The header carries a
timestamp that changes on every run regardless of whether the underlying
Notion data did — hashing it in would make every run look changed and defeat
the point of the gate.

## What's done and what isn't

This is step 1 of a larger roadmap. All three session types are fully wired:
`prompts/class.txt`, `prompts/floor.txt` and `prompts/explore.txt` are real
prompts, and `condense._invoke_llm()` calls Gemini 3.8 Flash on Vertex AI for any
of them. The templates load, `{RAW_NOTES}` is substituted, and the model's reply
is what lands in Notion.

An empty prompt file means passthrough — `condense()` skips the model call and the
raw notes become the page body as-is. That is how `/explore` ran before its prompt
was written, and it is how a new session type can ship ahead of its prompt.

Voice notes are wired too — see "Voice notes" below. They're an addition, not a
replacement: you can type, speak, or mix both inside one session.

Also deliberately left alone: the `Exercises completed` multi-select on the Floor
barre database. Deciding which exercises a session covered is condensing work.

### Writing the prompts

Each file in `prompts/` is sent to the model verbatim, with `{RAW_NOTES}` replaced
by the session's messages joined with newlines, in the order they were sent. Three
things the prompt has to get right, because nothing downstream cleans up after it:

- **It must contain the literal `{RAW_NOTES}`.** Without it the notes never reach
  the model.
- **Output must be markdown**, since Notion parses the reply into blocks server-side.
- **No preamble and no sign-off.** The first characters of the reply are the first
  characters of the page — there's no title line to hide behind.

Keep the files genuinely separate rather than factoring out a shared base.
The point of splitting them is that a class and a floor barre can ask different
things of the notes without one prompt trying to serve both — and they now do:
class notes are named steps, floor barre is numbered Kniaseff exercises, and
exploration is a movement map with no corrections at all.

### Voice notes

Send a Telegram voice note instead of typing, or alongside typing. Each one is
transcribed **on arrival**, not at `/done`, and the transcript is buffered exactly
as a typed message would be. Nothing downstream knows the difference — `condense()`,
`buffer.py` and `notion.py` all just see strings.

On arrival rather than at `/done` for a concrete reason: `buffer.py` keeps a session's
chunks in a single Firestore document, and Firestore caps a document at 1 MiB. Audio
doesn't fit. Transcribing first keeps the buffer holding strings, so mixed
typed-and-spoken sessions work for free. Each note is stored with its Telegram
`message_id` and sorted by it at `/done`, so a slow transcription that finishes
after a later typed note still lands in the order you sent them.

Transcription is a second Gemini call using `prompts/transcribe.txt`, with
`prompts/vocab.txt` substituted into it at `{VOCAB}` — a ballet vocabulary list ending
in a misheard-terms map written `correct term → what it gets misheard as`. That map is
doing most of the work: without it, `grande extensions` came back as `groin extensions`.
Proper nouns need to be listed explicitly; domain context alone doesn't fix them
(`Paquita` transcribed as "piquita" until it was added).

Two things the transcription prompt must keep doing, both load-bearing:

- **Preserve filler, hedging and self-correction.** The condensing prompts read them
  as signal — unresolved talk becomes `(watch)`, "like last time" becomes `(recurring)`,
  "maybe placebo" becomes Try next time. Tidying the speech disarms those rules.
- **Write exercise numbers as words** (`exercise one`, never `exercise 1`). Runs drifted
  between the two formats, and `prompts/floor.txt` groups bullets by exercise, so an
  unstable label means unstable grouping.

Telegram caps a bot's file download at 20MB, which is under the inline request limit,
so audio is always sent inline — no bucket, no `getFile` size handling.

### The model call

`condense._invoke_llm()` calls **Gemini 3.8 Flash** on Vertex AI (`gemini-3.8-flash`,
via the `google-genai` SDK, `vertexai=True`). Requires:

- `aiplatform.googleapis.com` enabled (see 5a) — no separate Model Garden access
  request needed, unlike Anthropic models on Vertex;
- `VERTEX_PROJECT` / `VERTEX_LOCATION` env vars (set in `deploy.sh`).
  `VERTEX_LOCATION` defaults to `"global"`, which sidesteps Vertex's per-region
  model availability — no need to check whether a given region carries this model;
- `google-genai` in `requirements.txt`;
- the runtime service account holding `roles/aiplatform.user` (see 5b).

No secret is needed — Vertex authenticates as the function's service account.

The client sets a 500s request timeout (`condense.py`), 40s under the function's
540s limit, so a slow model call surfaces as a caught exception with a Telegram
error reply instead of a hard Cloud Functions kill that leaves the user hanging
with no response at all.

These were 100s and 120s originally, which turned out to be too tight: a long
`/done` returned `504 DEADLINE_EXCEEDED` from Vertex, and transcribing a long voice
note is slower still. Telegram retries a webhook it doesn't get a fast 200 from, but
that's already handled — `update_id` is claimed transactionally before any model
call, so the retry is dropped and only the original invocation replies.

## Prerequisites

1. **Telegram bot** — message `@BotFather` → `/newbot` → copy the bot token.
   Message `@userinfobot` to get your own numeric Telegram user id.
2. **Notion integration** — [notion.so/my-integrations](https://notion.so/my-integrations)
   → New internal integration → copy the secret token.
3. **Three Notion databases** — see 3a below for the exact properties they need.
4. **Share every database with the integration** — open each one in Notion →
   `...` menu → Connections → add your integration. Without this the API returns
   404, and it's easy to do for one database and forget another.
5. **GCP account with billing enabled, and the `gcloud` CLI installed**
   (`brew install --cask google-cloud-sdk`). See 5a and 5b below — 5b is the part
   that will otherwise fail your first deploy.

### 3a. The three databases

Create each by hand in Notion. Property names must match **exactly**, including
capitalisation — `notion.py` addresses them by literal string, so `Raw Notes`
instead of `Raw notes` is a 400 at write time.

| Database             | Properties                                                        |
|----------------------|-------------------------------------------------------------------|
| Ballet class notes   | `Name` (title), `Date` (date), `Raw notes` (text)                 |
| Floor barre notes    | the same three, plus `Exercises completed` (multi-select)         |
| Movement exploration | `Name` (title), `Date` (date), `Raw notes` (text)                 |

`Name` is the default title property every Notion database starts with — you only
have to add the rest. The `Exercises completed` options are yours to define; the
bot never writes to it.

Then grab each database's **data source id**: `...` menu → Manage data sources →
Copy data source ID. You'll need all three in `deploy.sh`. Note this is *not* the id in
the database's URL — those are different objects.

### 5a. Point gcloud at the project and turn on the APIs

```bash
gcloud auth login
gcloud config set project project-69fd2b15-f478-43ca-b5d

gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  aiplatform.googleapis.com

# Firestore, Native mode. One database per project, and the mode can never be
# changed afterwards — if this project already uses Datastore mode, use a new project.
gcloud firestore databases create --location=asia-south1
```

### 5b. Store the secrets and grant the service account access

Pipe secrets from stdin rather than passing them as arguments, so they don't land
in your shell history:

```bash
printf '%s' 'ntn_...' | gcloud secrets create notion-token       --data-file=-
printf '%s' '<bot token>' | gcloud secrets create telegram-bot-token --data-file=-
printf '%s' "$(openssl rand -hex 20)" | gcloud secrets create telegram-webhook-secret --data-file=-
```

The webhook secret is just a random string — you'll pass the same value to
Telegram in the "Register the webhook" step below.

Now the permissions. The default compute service account both **runs** the
function and **builds** it, and a new project grants it neither automatically:

```bash
PROJECT_NUMBER=$(gcloud projects describe project-69fd2b15-f478-43ca-b5d --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Runtime: read the secrets, read and write Firestore.
for S in notion-token telegram-bot-token telegram-webhook-secret; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor
done
gcloud projects add-iam-policy-binding project-69fd2b15-f478-43ca-b5d \
  --member="serviceAccount:${SA}" --role=roles/datastore.user
gcloud projects add-iam-policy-binding project-69fd2b15-f478-43ca-b5d \
  --member="serviceAccount:${SA}" --role=roles/aiplatform.user

# Build: fetch the source, push the image, write build logs.
for ROLE in roles/storage.objectViewer roles/logging.logWriter roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding project-69fd2b15-f478-43ca-b5d \
    --member="serviceAccount:${SA}" --role="$ROLE"
done
```

Skip that second block and the first deploy dies before it reaches any of your
code, with an error that doesn't name the missing role:

```
ERROR: (gcloud.functions.deploy) OperationError: code=3, message=Build failed with
status: FAILURE. Could not build the function due to a missing permission on the
build service account.
```

### 5c. Firestore TTL (optional cleanup)

Sessions and dedup markers carry an `expireAt` timestamp. Correctness doesn't
depend on this — `buffer.py` treats a past `expireAt` as gone on every read — it's
housekeeping, and Firestore deletes up to ~24h late. Open sessions expire after 7
days; finished and discarded ones are kept 30 days as markers so their topics
can't be reused.
Not currently applied on the live deployment.

```bash
gcloud firestore fields ttls update expireAt \
  --collection-group=buffers --enable-ttl
```

## NotebookLM sync setup (one-time, do before deploying the sync)

The Notion→Drive→NotebookLM pipeline needs five one-time steps outside this
repo, done in order:

1. **Enable the Drive and IAM Credentials APIs** on the same GCP project
   everything else runs in:
   ```bash
   gcloud services enable drive.googleapis.com iamcredentials.googleapis.com \
     --project=project-69fd2b15-f478-43ca-b5d
   ```
2. **Create three Google Docs yourself** — Drive → New → Google Doc, leave them
   empty, name them whatever you want to see in NotebookLM (e.g.
   "Ballet — Class Notes", "Ballet — Floor Barre Notes" and
   "Ballet — Movement Exploration Notes"). They must be owned
   by your real Google account, not the service account: a service account on
   a consumer Gmail account has no usable Drive storage, and a NotebookLM
   source has to be a file you can see in your own Drive picker. Copy each
   file ID from its URL (`docs.google.com/document/d/<FILE_ID>/edit`) into
   `DRIVE_CLASS_DOC_ID`, `DRIVE_FLOOR_DOC_ID` and `DRIVE_EXPLORE_DOC_ID` in
   `deploy.sh`.
3. **Share every Doc with the runtime service account as Editor**:
   ```bash
   PROJECT_NUMBER=$(gcloud projects describe project-69fd2b15-f478-43ca-b5d --format='value(projectNumber)')
   echo "${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
   ```
   For each Doc: Share → paste that address → Editor → uncheck "Notify
   people". This is where the sync's write access actually comes from — it's
   a Drive-level ACL, not a GCP IAM role, so nothing in `gcloud` grants it.
   Skip it and the Drive write 404s on a file that plainly exists.
4. **Grant the runtime service account permission to impersonate itself**,
   which is how it gets a Drive-scoped token (see "Why the Drive auth is
   impersonation, not plain ADC" above):
   ```bash
   PROJECT_NUMBER=$(gcloud projects describe project-69fd2b15-f478-43ca-b5d --format='value(projectNumber)')
   SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

   gcloud iam service-accounts add-iam-policy-binding "$SA" \
     --project=project-69fd2b15-f478-43ca-b5d \
     --member="serviceAccount:${SA}" \
     --role=roles/iam.serviceAccountTokenCreator
   ```
   IAM policy changes can take a minute or two to propagate — a `403
   PERMISSION_DENIED` on `generateAccessToken` right after running this is
   expected transiently; retry after a short wait before assuming something
   is actually wrong.
5. **Turn on "Read content" on the Notion integration** — it has so far only
   ever written pages. notion.so → Settings → Connections → your integration
   → Capabilities → check **Read content**. Without this, `GET
   /pages/{id}/markdown` 403s. Also confirm the integration is connected to
   **every** database (see "3a. The three databases" above) — a missing
   connection here 404s instead.

**Add the Docs to NotebookLM only after the first successful `/sync`** — an
empty Doc added as a source indexes as empty, and you'd otherwise be relying
on auto-sync to pick up the very first write. Once you have added them:
notebook → Add source → Google Drive → pick each Doc, once. After that,
NotebookLM's automatic Drive sync keeps them current with no further action
on either side.

## Confirm the Notion databases (before deploying)

```bash
NOTION_TOKEN=$(gcloud secrets versions access latest --secret=notion-token \
  --project=project-69fd2b15-f478-43ca-b5d)

for ID in 3aa3ef88-7ec8-8075-95b7-000b8a6396eb 6c63ef88-7ec8-822b-b773-8786b5e160d0 \
          3e43ef88-7ec8-8092-bd56-000b11bad5db; do
  curl -s "https://api.notion.com/v1/data_sources/$ID" \
    -H "Authorization: Bearer $NOTION_TOKEN" \
    -H "Notion-Version: 2026-03-11" | python3 -m json.tool | head -30
done
```

Expect each response's `properties` to contain `Name`, `Date` and `Raw notes`
(and `Exercises completed` on the floor barre one). If you get a 404, redo
prerequisite 4 — the integration isn't connected to that database. Doing this
first is much faster than finding out from a failed `/done` after a class.

## Deploy

```bash
export ALLOWED_CHAT_ID=<your numeric telegram id>
./deploy.sh
```

`deploy.sh` holds the non-secret configuration inline, so a deploy is fully
described by that one file. If you're standing this up on a different GCP project
or against different Notion databases, the values to change are all at the top:

- `PROJECT_ID`, `REGION`, `FUNCTION_NAME`
- `NOTION_CLASS_DATA_SOURCE_ID`, `NOTION_FLOOR_DATA_SOURCE_ID`,
  `NOTION_EXPLORE_DATA_SOURCE_ID` — from 3a above
- `DRIVE_CLASS_DOC_ID`, `DRIVE_FLOOR_DOC_ID`, `DRIVE_EXPLORE_DOC_ID` — the three
  Google Doc file IDs from the "NotebookLM sync setup" section above
- `DRIVE_IMPERSONATE_SA` — derived from `PROJECT_ID` at deploy time, so it needs
  no editing. It is the function's own runtime service account, which the
  function impersonates to obtain a Drive-scoped token (see "Notes on the
  design")
- `LOCAL_TZ` — defaults to `Asia/Kolkata`; used for the Date field and the
  time in topic names, since the function itself runs in UTC
- `NOTION_VERSION` — leave at `2026-03-11` or later. The page-body `markdown`
  field doesn't exist in older versions, so an earlier value still creates the
  page and its properties but silently drops the condensed entry.

Secrets are never in the file: they're mounted from Secret Manager at runtime.
`ALLOWED_CHAT_ID` comes from the environment so the script stays shareable.

The script prints the function URL when it finishes. The current deployment is:

```
https://ballet-notes-bot-4mjqzwf6pq-el.a.run.app
```

Re-running `./deploy.sh` after a code change is all a redeploy takes.

If you deploy a change to how `buffer.py` stores sessions, finish or `/quit` any
open session first — a session stored under an old layout won't be found by the
new code. (This applied to the switch to topics: the old one-per-chat `buf_<chat_id>`
documents are no longer read.)

## Register the Telegram webhook

Read both values back out of Secret Manager rather than retyping them:

```bash
BOT_TOKEN=$(gcloud secrets versions access latest --secret=telegram-bot-token \
  --project=project-69fd2b15-f478-43ca-b5d)
WEBHOOK_SECRET=$(gcloud secrets versions access latest --secret=telegram-webhook-secret \
  --project=project-69fd2b15-f478-43ca-b5d)

curl "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=https://ballet-notes-bot-4mjqzwf6pq-el.a.run.app" \
  -d "secret_token=${WEBHOOK_SECRET}"

curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

The second command should show your URL with `pending_update_count: 0` and no
`last_error_message`. Don't add `allowed_updates` to `setWebhook` unless it
includes `edited_message` — editing a sent note relies on it.

## Turn on topics (one-time)

Each session gets its own Telegram topic, so several can be open at once — start
a floor barre, then a class, and keep adding to both. This needs the bot's
threaded mode on:

1. Open @BotFather and tap **Open** (bottom left) to launch its mini app — the
   setting isn't in the typed `/mybots` menu.
2. Pick the bot → **Bot Settings** → **Threads Settings** → turn **Threaded Mode** on.
3. Update Telegram, then reopen the chat with the bot: it now shows a topic list
   with **General**.

The bot checks this setting itself (`getMe` → `has_topics_enabled`, cached 10
minutes). With threaded mode off it falls back to one session per chat, exactly
as before topics.

## Using it

1. In **General**, send `/class`, `/floor` or `/explore`. The bot opens a topic
   named e.g. `Floor barre · Oct 18, 10:05` and starts the session there.
   (Or create a topic yourself and send the command inside it.)
2. Inside that topic, send your raw notes, split across as many messages as you
   like (Telegram caps a single message at 4096 characters). Voice notes work too,
   and can be mixed freely with typed messages — each is transcribed as it arrives
   and the bot replies `🎙 Transcribed (N message(s) buffered)`. Editing a sent
   note updates it, while the session is open.
3. Send `/done` inside the topic.

The bot replies `✅ Added to Notion` with a link in the topic, and renames it
`✅ Floor barre · Oct 18, 10:05`. The page is dated by when the session *started*,
so one finished after midnight lands on the right day.

Several sessions can be open at once — each topic is its own session. Other commands:

- `/sessions` — list the open sessions, anywhere.
- `/quit` inside a topic — discard its notes; the topic is renamed `🗑 …` and kept,
  never deleted.
- `/start` or `/help` — usage instructions.

Rules that keep notes from landing in the wrong place:

- Notes, voice notes, `/done` and `/quit` sent in **General** are refused with the
  list of open sessions — the bot won't guess which session they belong to.
- A topic holds one session of one type: `/class` inside a floor barre topic is
  refused, and a finished (`✅`) or discarded (`🗑`) topic can't be reused.
- A second `/done` while one is saving is refused, and so is a note sent mid-save.
- If `/done` fails, the notes are kept — send `/done` again to retry.
- An open session expires after 7 days without a message. Notes sent to an
  expired topic are refused with an explanation.
- Notes are stored in the order sent (by Telegram `message_id`), even if a slow
  voice transcription finishes after a later typed note.

### NotebookLM

Send `/sync` any time to rebuild every Google Doc from the current state of
its Notion database — right after a class, or whenever you want NotebookLM
caught up. The bot replies `Syncing Notion → Google Docs…`, then a per-Doc
summary:

```
✅ Sync done
Class: updated (14 sessions)
Floor barre: already up to date (9 sessions)
Exploration: updated (3 sessions)
```

"Already up to date" means the content hash matched the last sync and the
Drive write was skipped — normal, not an error. Add every Doc to a NotebookLM
notebook once (see "NotebookLM sync setup" above); after that every `/sync`
that changes a Doc propagates on its own, no re-add needed.

## Local testing

The examples below have no topic fields, so with threaded mode on they count as
**General**: `/class` will really create a topic in your Telegram chat, and notes
or `/done` without a topic are refused. To post into a topic, add
`"message_thread_id":<topic id>,"is_topic_message":true` to the message (the
topic id is in the logs, or in the `sess_…` Firestore document).


Needs Python 3.10 or later (the deployed runtime is 3.12) — `notion_read.py` uses
`str | None`, which fails at import on 3.9.

Firestore needs application default credentials locally; without them the client
fails on the first request:

```bash
gcloud auth application-default login

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=test-secret ALLOWED_CHAT_ID=...
export NOTION_TOKEN=... NOTION_CLASS_DATA_SOURCE_ID=... NOTION_FLOOR_DATA_SOURCE_ID=...
export NOTION_EXPLORE_DATA_SOURCE_ID=...
export DRIVE_CLASS_DOC_ID=... DRIVE_FLOOR_DOC_ID=... DRIVE_EXPLORE_DOC_ID=...
export DRIVE_IMPERSONATE_SA=<project number>-compute@developer.gserviceaccount.com
export GOOGLE_CLOUD_PROJECT=project-69fd2b15-f478-43ca-b5d
export VERTEX_PROJECT=project-69fd2b15-f478-43ca-b5d

functions-framework --target=main --debug
```

Then in another shell:

```bash
post() {
  curl -s localhost:8080 -H 'Content-Type: application/json' \
    -H 'X-Telegram-Bot-Api-Secret-Token: test-secret' -d "$1"
}
post '{"update_id":1,"message":{"chat":{"id":<your id>},"text":"/class"}}'
post '{"update_id":2,"message":{"chat":{"id":<your id>},"text":"plies felt shaky today"}}'
post '{"update_id":3,"message":{"chat":{"id":<your id>},"text":"/done"}}'
```

This talks to real Firestore and real Notion — it creates a real page. Delete it
afterwards. Bump `update_id` each run, or the dedup check will ignore the request.

Local runs share the deployed function's Firestore collection by default, so a
half-finished test session can collide with a real one. Set
`FIRESTORE_COLLECTION=buffers-dev` to keep them apart — `buffer.py` reads it, and
nothing else needs to know.

## Troubleshooting

**Deploy fails with a missing build permission** — see 5b; the compute service
account doesn't have the build roles. The build log won't help: it fails at the
source-fetch step before your code runs.
```
Could not build the function due to a missing permission on the build service account.
```

**Deploy warns about a missing Cloud Run service** — harmless. It appears when an
earlier deploy failed partway and left nothing behind. The deploy printing it
still succeeded; confirm with `state: ACTIVE` in its output.
```
[WARNING] Cloud Run service .../services/ballet-notes-bot for the function was not
found. The service was redeployed with default values.
```

**The bot replies with a generic error** — get the real traceback:
```bash
gcloud functions logs read ballet-notes-bot --gen2 \
  --project=project-69fd2b15-f478-43ca-b5d --region=asia-south1 --limit=50
```

**Notion 404** — the integration isn't connected to that database. Redo
prerequisite 4 for whichever one failed, then re-run the confirm step above.

**Notion 400 on `/done`** — usually a property name mismatch. The database needs
`Name`, `Date` and `Raw notes` spelled exactly that way (see 3a).

**`/class` says "Couldn't open a topic"** — threaded mode is off in BotFather, or
the bot isn't allowed to create topics there. Check `getMe` shows
`"has_topics_enabled": true`. The bot only re-checks every 10 minutes, so wait
after changing the setting. Meanwhile you can create a topic by hand and send
the command inside it.

**Notes inside a topic get "…inside a session's topic, not in General"** — Telegram isn't marking
those messages `is_topic_message`. Check the `update_id=… topics=…` log line; the
rule lives at the top of `main()` in `main.py`.

**Nothing happens at all** — check `getWebhookInfo` for `last_error_message`, and
confirm the `secret_token` you registered matches the `telegram-webhook-secret`
value in Secret Manager. A mismatch is silently ignored by design, so it looks
identical to the bot being down.

## Verification checklist

- [ ] The Notion confirm curl above returns the expected properties for every database.
- [ ] `./deploy.sh` succeeds and prints a function URL.
- [ ] `getWebhookInfo` shows no `last_error_message`.
- [ ] End-to-end: `/class` in General → 2-3 messages of real notes in its topic →
      `/done` there → bot confirms and a new page appears in **Ballet class notes**
      with the session's start date, a month-and-day title, and the raw notes preserved.
- [ ] Same again with `/floor` → the page lands in **Floor barre notes** instead.
- [ ] Same again with `/explore` → the page lands in **Movement exploration**, with
      the body starting at **Awareness**.
- [ ] Voice: `/floor` → send a voice note → bot replies `🎙 Transcribed` → `/done` →
      the Notion page is condensed, ballet terms are spelled correctly, and `Raw notes`
      holds the transcript with its filler intact.
- [ ] Mixed: `/floor` → type a message → send a voice note → type again → `/done` →
      all three appear in the entry, in the order sent.
- [ ] Voice note sent in General, or in a topic with no session → refused, and
      does *not* burn a transcription call.
- [ ] Notes sent in a topic with no session → refused; nothing buffered.
- [ ] `/quit` mid-session → notes discarded, no page created.
- [ ] Topics: `/floor` in General opens a topic; `/class` in General opens a second;
      notes in each → `/done` in each → two pages, each with only its own notes.
- [ ] `/class` inside the floor topic → refused; floor session unchanged.
- [ ] Note in General → refused with the open-session list.
- [ ] Double-tap `/done` → one page only.
- [ ] Logs show `is_topic_message=True` for messages inside a topic and `None` in
      General (the `update_id=… topics=…` line).
- [ ] Security: POST to the function URL without the `X-Telegram-Bot-Api-Secret-Token`
      header → silently ignored (200, no page). Message the bot from a different
      Telegram account → "Not authorized", no page.

Notion→Drive→NotebookLM pipeline (do the "NotebookLM sync setup" steps first):

- [ ] `/sync` → replies `Syncing Notion → Google Docs…`, then `✅ Sync done` with a
      line per database and session counts matching the row counts in Notion.
- [ ] Every Google Doc contains real formatting — H1 title, `##` session headings,
      horizontal rules — not literal `#` characters. Literal `#`s mean the markdown
      conversion failed; see the `_MEDIA_MIME` fallback ladder in `drive.py`.
- [ ] A floor barre session with `Exercises completed` tagged by hand shows a
      `**Exercises completed:** ...` line under its heading; untagged sessions don't.
- [ ] `/sync` again immediately → all report "already up to date" — the hash gate
      working. If it says "updated" twice in a row with no Notion change, something
      run-varying leaked into the hashed content (check `drive_sync.py`'s
      `_render_sessions` vs `_render`).
- [ ] Firestore → `buffers` collection → `sync_class`, `sync_floor` and
      `sync_explore` documents exist, each with `contentHash` and `syncedAt`, and
      no `expireAt`.
- [ ] After adding every Doc to a NotebookLM notebook, ask it a question spanning
      session types and confirm it cites more than one source.

## Notes on the design

- **Session type is an explicit command.** `/class`, `/floor` and `/explore` set it before any
  notes are buffered, rather than a button at `/done` or inferring it from the
  notes. A button means handling a second Telegram update type for a decision you
  can state in one word; inference means an extra model call that can be wrong.
- **Buffering**: raw notes routinely exceed Telegram's 4096-char message cap, so
  messages accumulate in Firestore until `/done` (or are dropped by `/quit`).
  Abandoned sessions expire after 7 days of silence — long, because with topics a
  session is often left open across hours or overnight.
- **One session per topic.** The topic a message is sent in decides its session,
  so nothing needs to remember an "active" session. Only messages Telegram marks
  `is_topic_message` count as in a topic: a plain reply can carry a
  `message_thread_id` too, and must not split off into its own session. Anything
  in General that needs a session is refused rather than guessed.
- **Topics are single-use.** A finished or discarded topic keeps a 30-day marker
  and can't start a new session, so a `✅` topic never quietly collects a second
  entry. A different session type inside an open topic is refused, not relabelled
  — relabelling would move the notes to another database.
- **`/done` locks before it works.** It sets the session to `finishing` in a
  transaction, then condenses and writes to Notion, and only then marks it `done`.
  A second `/done`, or a note, arriving mid-save is refused instead of making a
  duplicate page or being wiped. If the save fails the session reopens with its
  notes. A crash mid-save leaves it `finishing`; after 10 minutes (longer than the
  function's 540s timeout) a new `/done` may take it over.
- **Replies never fail the work.** `telegram.send_message` logs instead of raising,
  and falls back to the main chat if the topic was deleted, so a lost
  confirmation can't turn a saved page into a reported failure. Telegram errors
  are re-raised without the request URL, which contains the bot token.
- **Ordering**: chunks are appended with a read-modify-write transaction rather
  than `ArrayUnion`, which dedupes equal values — wrong here, since a repeated line
  may be something you genuinely wrote twice. Order comes from `message_id`
  at `/done`, not from append order.
- **Idempotency**: Telegram retries the webhook if it doesn't get a fast 200. Each
  `update_id` is claimed transactionally (1h TTL) so a slow model call can never
  produce two Notion pages.
- **Auth**: the function is deployed unauthenticated because Telegram can't sign
  requests any other way. The shared `secret_token` header is the entire gate;
  unsigned requests get a silent 200 and are dropped.
- **Chunking**: Notion caps a single rich_text object at 2000 characters, so long
  raw notes are split across several in the `Raw notes` property. The condensed
  markdown is *not* pre-chunked — splitting it mid-token would corrupt the syntax
  — Notion parses it into blocks server-side.
- **Prompts are files, not code**: one per session type, loaded at cold start.
  Splitting them up front is cheap and avoids one prompt trying to serve every
  session type later; if they end up identical, nothing is lost.
- **An empty prompt file means passthrough.** `/explore` shipped before its prompt
  was designed, so `prompts/explore.txt` started empty and `condense()` returned
  the raw notes unchanged instead of calling the model. The sessions still landed
  in Notion. A stub prompt would have filled the database with a format nobody
  chose. The rule stays in `condense()` for the next session type.
- **Voice is transcribed, not fed to the condenser as audio.** Handing the audio
  straight to `floor.txt` would save a call, but it leaves the Notion `Raw notes`
  property empty, and it forces audio through a buffer that can only hold strings.
  Splitting it also keeps the speech engine swappable: pass 1 is just "audio in, text
  out", so moving to Cloud Speech-to-Text with phrase boost later touches nothing else.
- **No separate voice prompt.** Tested against a real voice note: the existing
  `floor.txt` condensed a transcript correctly with no changes, because the narration
  is sequential and self-structured ("then for exercise two"). Two rules were added for
  defects the test exposed, but a parallel `floor-voice.txt` earned nothing.
- **Timezone data**: computing the Date field needs `zoneinfo` to resolve
  `LOCAL_TZ`, but slim Python runtimes often ship without the IANA timezone
  database. `tzdata` is in `requirements.txt` specifically so this resolves
  instead of raising `ZoneInfoNotFoundError` at runtime.
- **Drive auth impersonates the function's own service account.** The obvious
  thing — `google.auth.default(scopes=["...auth/drive"])` — does not work on
  Cloud Functions gen2. Gen2 runs on Cloud Run, whose metadata server issues
  tokens scoped to `cloud-platform`, and the `scopes=` argument cannot widen
  that: the metadata server only narrows to scopes the instance already has.
  `cloud-platform` covers Google *Cloud* APIs; Drive is a *Workspace* API and
  is not among them, so the call comes back `403 Request had insufficient
  authentication scopes` on a file that is visibly shared with the account —
  a scope problem wearing a permissions problem's clothes, which no amount of
  IAM-role or Drive-share fiddling fixes. `drive.py` instead uses the metadata
  credentials (which *can* reach `iamcredentials.googleapis.com`, since that
  is under `cloud-platform`) to mint a second token for the same service
  account with the Drive scope requested explicitly. Same identity, wider
  scope, and no service-account key anywhere. This is what the
  `roles/iam.serviceAccountTokenCreator` self-binding in the setup pays for.
- **The sync hash deliberately excludes the "Last synced" line.** Each Doc's
  header carries a timestamp, which changes on every run whether or not Notion
  did. `drive_sync.py` therefore hashes `_render_sessions()` — the session
  bodies alone — and not `_render()`, the full document. Hashing the full
  document would make every run look changed, the gate would never fire, and
  NotebookLM would re-index every Doc on every `/sync` for no reason.
- **Full rebuild every sync, not an incremental append.** There is no per-page
  state beyond one content hash per database. Each run re-reads a whole data
  source and re-renders its Doc from scratch, so edits, deletions and reorders
  made by hand in Notion all show up correctly and nothing can drift. The hash
  exists only to skip a pointless Drive write, never to decide what to send.
  The Drive write also happens *before* the hash is stored: reversed, a failed
  upload would still mark the run done and the Doc would drift forever.
- **`/sync` is manual on purpose.** Syncing inside `/done` would add the whole
  Notion-read and Drive-write round trip to every logged session, coupling the
  capture path — the one thing that must never fail — to Drive availability.
  A scheduled Cloud Scheduler job is the obvious upgrade if remembering `/sync`
  becomes a chore; `drive_sync.sync_all()` has no Telegram dependency, so it
  only needs a second entry point and a cron.
