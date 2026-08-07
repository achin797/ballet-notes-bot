# Ballet notes bot

Send raw notes to a Telegram bot after a ballet class or a floor barre session —
typed, spoken as voice notes, or both in the same session. It condenses them and
adds a page to the matching Notion database: title (month and day, e.g. "July 27"),
today's date, the raw notes, and the condensed entry as the page body.

```
raw notes -> Telegram bot -> Cloud Function (condense + write to Notion) -> Notion page
```

## Architecture

```
You (Telegram)
      │  /class or /floor, then raw notes — typed and/or voice notes — then /done
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
                                            New page in Ballet class notes
                                                     or Floor barre notes
```

| Piece           | Role                                                             | File                           |
|-----------------|------------------------------------------------------------------|--------------------------------|
| Telegram bot    | your only interface — send notes, get a confirmation + link back | `telegram.py`                  |
| Transcriber     | turns a voice note into the text a typed message would have been  | `condense.transcribe()`        |
| Cloud Function  | receives the webhook, routes commands, orchestrates the pipeline | `main.py`                      |
| Firestore       | holds a session's buffered messages until `/done`; dedupes retries | `buffer.py`                  |
| Condenser       | runs the per-session-type prompt over the raw notes               | `condense.py`, `prompts/*.txt` |
| Notion API      | creates the page: Name, Date, Raw notes + condensed body          | `notion.py`                    |

Everything runs on request — there's no server to keep up. Cloud Functions and
Firestore only cost anything while actually processing a message, which for this
use case (a few sessions a week) is effectively free.

## What's done and what isn't

This is step 1 of a larger roadmap. Both session types are fully wired:
`prompts/class.txt` and `prompts/floor.txt` are both real prompts, and
`condense._invoke_llm()` calls Gemini 3.6 Flash on Vertex AI for either one. The
templates load, `{RAW_NOTES}` is substituted, and the model's reply is what lands
in Notion.

Voice notes are wired too — see "Voice notes" below. They're an addition, not a
replacement: you can type, speak, or mix both inside one session.

Also deliberately left alone: the `Exercises completed` multi-select on the Floor
barre database. Deciding which exercises a session covered is condensing work.

### Writing the prompts

Each file in `prompts/` is sent to the model verbatim, with `{RAW_NOTES}` replaced
by the session's messages joined with newlines, in the order they arrived. Two
things the prompt has to get right, because nothing downstream cleans up after it:

- **Output must be markdown**, since Notion parses the reply into blocks server-side.
- **No preamble and no sign-off.** The first characters of the reply are the first
  characters of the page — there's no title line to hide behind.

Keep the two files genuinely separate rather than factoring out a shared base.
The point of splitting them is that a class and a floor barre can ask different
things of the notes without one prompt trying to serve both — and they now do:
class notes are named steps, floor barre is numbered Kniaseff exercises.

### Voice notes

Send a Telegram voice note instead of typing, or alongside typing. Each one is
transcribed **on arrival**, not at `/done`, and the transcript is buffered exactly
as a typed message would be. Nothing downstream knows the difference — `condense()`,
`buffer.py` and `notion.py` all just see strings.

On arrival rather than at `/done` for a concrete reason: `buffer.py` keeps a session's
chunks in a single Firestore document, and Firestore caps a document at 1 MiB. Audio
doesn't fit. Transcribing first keeps the buffer holding strings, so mixed
typed-and-spoken sessions work for free, in arrival order.

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

`condense._invoke_llm()` calls **Gemini 3.6 Flash** on Vertex AI (`gemini-3.6-flash`,
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
3. **Two Notion databases** — see 3a below for the exact properties they need.
4. **Share both databases with the integration** — open each one in Notion →
   `...` menu → Connections → add your integration. Without this the API returns
   404, and it's easy to do for one database and forget the other.
5. **GCP account with billing enabled, and the `gcloud` CLI installed**
   (`brew install --cask google-cloud-sdk`). See 5a and 5b below — 5b is the part
   that will otherwise fail your first deploy.

### 3a. The two databases

Create both by hand in Notion. Property names must match **exactly**, including
capitalisation — `notion.py` addresses them by literal string, so `Raw Notes`
instead of `Raw notes` is a 400 at write time.

| Database             | Properties                                                        |
|----------------------|-------------------------------------------------------------------|
| Ballet class notes   | `Name` (title), `Date` (date), `Raw notes` (text)                 |
| Floor barre notes    | the same three, plus `Exercises completed` (multi-select)         |

`Name` is the default title property every Notion database starts with — you only
have to add the rest. The `Exercises completed` options are yours to define; the
bot never writes to it.

Then grab each database's **data source id**: `...` menu → Manage data sources →
Copy data source ID. You'll need both in `deploy.sh`. Note this is *not* the id in
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

Abandoned sessions and dedup markers carry an `expireAt` timestamp. Correctness
doesn't depend on this — it's housekeeping, and Firestore deletes up to ~24h late.
Not currently applied on the live deployment.

```bash
gcloud firestore fields ttls update expireAt \
  --collection-group=buffers --enable-ttl
```

## Confirm the Notion databases (before deploying)

```bash
NOTION_TOKEN=$(gcloud secrets versions access latest --secret=notion-token \
  --project=project-69fd2b15-f478-43ca-b5d)

for ID in 3aa3ef88-7ec8-8075-95b7-000b8a6396eb 6c63ef88-7ec8-822b-b773-8786b5e160d0; do
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
- `NOTION_CLASS_DATA_SOURCE_ID`, `NOTION_FLOOR_DATA_SOURCE_ID` — from 3a above
- `LOCAL_TZ` — defaults to `Asia/Kolkata`; used to compute the Date field
  correctly, since the function itself runs in UTC
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
`last_error_message`.

## Using it

1. Send `/class` or `/floor` to tell the bot which kind of session it was.
2. Send your raw notes, split across as many messages as you like (Telegram caps
   a single message at 4096 characters). Voice notes work too, and can be mixed
   freely with typed messages — each is transcribed as it arrives and the bot
   replies `🎙 Transcribed (N message(s) buffered)`.
3. Send `/done`.

The bot replies `✅ Added to Notion` with a link once the page is created.

Other commands: `/start` or `/help` for usage instructions, `/quit` to discard the
current session and start over. Notes sent before `/class` or `/floor` are
refused rather than buffered — the bot won't guess which database they belong in.

## Local testing

Firestore needs application default credentials locally; without them the client
fails on the first request:

```bash
gcloud auth application-default login

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=test-secret ALLOWED_CHAT_ID=...
export NOTION_TOKEN=... NOTION_CLASS_DATA_SOURCE_ID=... NOTION_FLOOR_DATA_SOURCE_ID=...
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

**Nothing happens at all** — check `getWebhookInfo` for `last_error_message`, and
confirm the `secret_token` you registered matches the `telegram-webhook-secret`
value in Secret Manager. A mismatch is silently ignored by design, so it looks
identical to the bot being down.

## Verification checklist

- [ ] The Notion confirm curl above returns the expected properties for both databases.
- [ ] `./deploy.sh` succeeds and prints a function URL.
- [ ] `getWebhookInfo` shows no `last_error_message`.
- [ ] End-to-end: `/class` → 2-3 messages of real notes → `/done` → bot confirms
      and a new page appears in **Ballet class notes** with today's date, a
      month-and-day title, and the raw notes preserved.
- [ ] Same again with `/floor` → the page lands in **Floor barre notes** instead.
- [ ] Voice: `/floor` → send a voice note → bot replies `🎙 Transcribed` → `/done` →
      the Notion page is condensed, ballet terms are spelled correctly, and `Raw notes`
      holds the transcript with its filler intact.
- [ ] Mixed: `/floor` → type a message → send a voice note → type again → `/done` →
      all three appear in the entry, in the order sent.
- [ ] Voice note sent before `/class` or `/floor` → bot asks which session, and does
      *not* burn a transcription call.
- [ ] Notes sent before `/class` or `/floor` → bot asks which session; nothing buffered.
- [ ] `/quit` mid-session → notes discarded, no page created.
- [ ] Security: POST to the function URL without the `X-Telegram-Bot-Api-Secret-Token`
      header → silently ignored (200, no page). Message the bot from a different
      Telegram account → "Not authorized", no page.

## Notes on the design

- **Session type is an explicit command.** `/class` and `/floor` set it before any
  notes are buffered, rather than a button at `/done` or inferring it from the
  notes. A button means handling a second Telegram update type for a decision you
  can state in one word; inference means an extra model call that can be wrong.
- **Buffering**: raw notes routinely exceed Telegram's 4096-char message cap, so
  messages accumulate in Firestore until `/done` (or are dropped by `/quit`).
  Abandoned sessions self-expire after 6 hours.
- **Ordering**: chunks are appended with a read-modify-write transaction rather
  than `ArrayUnion`, which dedupes equal values and makes no ordering promise —
  both wrong here, since arrival order is the point and a repeated line may be
  something you genuinely wrote twice.
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
  Splitting them up front is cheap and avoids one prompt trying to serve both
  session types later; if they end up identical, nothing is lost.
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
