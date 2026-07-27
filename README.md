# Ballet notes bot

Send raw notes to a Telegram bot after a ballet class or a floor barre session.
It condenses them and adds an entry to the matching Notion database.

```
You (Telegram)
      │  /class or /floor, then raw notes, then /done
      ▼
Telegram Bot API
      │  webhook POST on every message
      ▼
Cloud Function (gen2)  — main.py orchestrates everything below
      │
      ├─ buffers messages & dedupes retried webhooks ──▶ Firestore
      ├─ condenses the notes on /done ─────────────────▶ prompts/ + LLM  (STUBBED)
      └─ writes the finished entry ────────────────────▶ Notion API
                                                              │
                                                              ▼
                                            New page in Ballet class notes
                                                     or Floor barre notes
```

| Piece            | Role                                                        | File                          |
|------------------|-------------------------------------------------------------|-------------------------------|
| Telegram bot     | the only interface — send notes, get a link back            | `telegram.py`                 |
| Cloud Function   | receives the webhook, routes commands, runs the pipeline    | `main.py`                     |
| Firestore        | holds a session's notes until `/done`; dedupes retries       | `buffer.py`                   |
| Condenser        | runs the per-session-type prompt over the raw notes          | `condense.py`, `prompts/*.txt`|
| Notion API       | creates the page: Name, Date, Raw notes + condensed body     | `notion.py`                   |

Everything runs on request. Cloud Functions and Firestore only cost anything
while actually processing a message, which for a few sessions a week is
effectively free.

## Status: step 1 of a larger roadmap

The pipeline is complete end to end **except the condensing itself**:

- `prompts/class.txt` and `prompts/floor.txt` are placeholders.
- `condense._invoke_llm()` does not call a model — it echoes the notes back.

Everything around that is real and exercised: templates are loaded, `{RAW_NOTES}`
is substituted, and the result is what lands in Notion. Finishing the condenser
means writing the two prompts and replacing that one function with a Vertex AI
(Claude) call. Nothing else has to change.

Also deliberately left alone: the `Exercises completed` multi-select on the Floor
barre database. Deciding which exercises a session covered is condensing work.

## The two databases

Both already exist and share the same three properties, so `session_type` only
picks which one to write to:

| Database          | Properties                                                    |
|-------------------|---------------------------------------------------------------|
| Ballet class notes| `Name` (title), `Date` (date), `Raw notes` (text)             |
| Floor barre notes | the same three, plus `Exercises completed` (multi-select)     |

Data source ids are set in `deploy.sh`.

## Setup

### 1. Telegram bot

- Message `@BotFather` → `/newbot` → copy the bot token.
- Message `@userinfobot` to get your numeric user id — that becomes `ALLOWED_CHAT_ID`.

### 2. Notion integration

- [notion.so/my-integrations](https://notion.so/my-integrations) → new internal
  integration → copy the secret.
- Open **each** database → `...` → Connections → add the integration. Without
  this the API returns 404.

### 3. GCP project

```bash
gcloud config set project project-69fd2b15-f478-43ca-b5d

gcloud services enable \
  cloudfunctions.googleapis.com \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com

# Firestore, Native mode. One per project, and the mode cannot be changed later.
gcloud firestore databases create --location=asia-south1
```

### 4. Secrets

Never pass these on a command line that lands in shell history — pipe from stdin:

```bash
printf '%s' 'ntn_...'  | gcloud secrets create notion-token            --data-file=-
printf '%s' '<token>'  | gcloud secrets create telegram-bot-token      --data-file=-
printf '%s' "$(openssl rand -hex 20)" | gcloud secrets create telegram-webhook-secret --data-file=-
```

Grant the function's runtime service account read access to each:

```bash
PROJECT_NUMBER=$(gcloud projects describe project-69fd2b15-f478-43ca-b5d --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

for S in notion-token telegram-bot-token telegram-webhook-secret; do
  gcloud secrets add-iam-policy-binding "$S" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor
done

gcloud projects add-iam-policy-binding project-69fd2b15-f478-43ca-b5d \
  --member="serviceAccount:${SA}" --role=roles/datastore.user
```

### 5. Firestore TTL (optional cleanup)

Abandoned sessions and dedup markers carry an `expireAt` timestamp. Correctness
does not depend on TTL — it is housekeeping, and Firestore deletes up to ~24h late.

```bash
gcloud firestore fields ttls update expireAt \
  --collection-group=buffers --enable-ttl
```

### 6. Deploy

```bash
export ALLOWED_CHAT_ID=<your numeric telegram id>
./deploy.sh
```

Note the printed function URL.

### 7. Register the webhook

```bash
BOT_TOKEN=<your bot token>
WEBHOOK_SECRET=<the same random string you stored in Secret Manager>

curl "https://api.telegram.org/bot${BOT_TOKEN}/setWebhook" \
  -d "url=<function url from deploy.sh>" \
  -d "secret_token=${WEBHOOK_SECRET}"

curl "https://api.telegram.org/bot${BOT_TOKEN}/getWebhookInfo"
```

The second call should show your URL, `pending_update_count: 0`, and no
`last_error_message`.

## Using it

1. `/class` or `/floor`
2. your raw notes, as many messages as you like
3. `/done`

The bot replies with a link to the new Notion page. `/quit` discards the current
session. `/start` or `/help` repeats the instructions.

## Local testing

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=... TELEGRAM_WEBHOOK_SECRET=test-secret ALLOWED_CHAT_ID=...
export NOTION_TOKEN=... NOTION_CLASS_DATA_SOURCE_ID=... NOTION_FLOOR_DATA_SOURCE_ID=...
export GOOGLE_CLOUD_PROJECT=project-69fd2b15-f478-43ca-b5d

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
afterwards.

## Troubleshooting

**Bot replies with the generic error** — get the traceback:

```bash
gcloud functions logs read ballet-notes-bot \
  --project=project-69fd2b15-f478-43ca-b5d --region=asia-south1 --gen2 --limit=50
```

**Notion 404** — the integration is not connected to that database. Redo step 2
for the database that failed.

**Nothing happens at all** — check `getWebhookInfo` for `last_error_message`, and
confirm the `secret_token` you registered matches the `telegram-webhook-secret`
value in Secret Manager. A mismatch is silently ignored by design.

## Verification checklist

- [ ] `/class` + notes + `/done` → page in **Ballet class notes** with today's
      date, a "Month Day" title, and the raw notes preserved.
- [ ] `/floor` + notes + `/done` → page in **Floor barre notes**, same shape.
- [ ] Notes sent before `/class` or `/floor` → bot asks which session; nothing buffered.
- [ ] `/quit` mid-session → notes discarded, no page created.
- [ ] POST to the function URL with no `X-Telegram-Bot-Api-Secret-Token` → 200,
      no page created.
- [ ] Message the bot from a different Telegram account → "Not authorized", no page.

## Notes on the design

- **Buffering**: raw notes routinely exceed Telegram's 4096-char message cap, so
  messages accumulate in Firestore until `/done`. Sessions self-expire after 6h.
- **Ordering**: chunks are appended with a read-modify-write transaction rather
  than `ArrayUnion`, which dedupes equal values and promises no order — both
  wrong here, since arrival order is the point and a repeated line may be real.
- **Idempotency**: Telegram retries a webhook that doesn't get a fast 200. Each
  `update_id` is claimed transactionally (1h TTL), so a slow LLM call can never
  produce two Notion pages.
- **Auth**: the function is deployed unauthenticated because Telegram cannot sign
  requests any other way. The shared `secret_token` header is the entire gate;
  unsigned requests get a silent 200 and are dropped.
- **Chunking**: Notion caps a rich_text object at 2000 characters, so raw notes
  are split across several. The condensed markdown is *not* pre-chunked —
  splitting it mid-token would corrupt the syntax — Notion parses it server-side.
- **Timezone data**: resolving `Asia/Kolkata` needs the IANA database, which slim
  Python runtimes often omit. `tzdata` is in `requirements.txt` for exactly that.
