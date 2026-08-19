#!/usr/bin/env bash
# Deploy the bot. Idempotent — re-run after any code change.
#
# Secrets live in Secret Manager and are mounted as env vars at runtime; they are
# never in this file, in the source, or in git. Non-secret config is inline below
# so a deploy is fully described by this one script.
set -euo pipefail

PROJECT_ID="project-69fd2b15-f478-43ca-b5d"
REGION="asia-south1"
FUNCTION_NAME="ballet-notes-bot"

# Your numeric Telegram user id (from @userinfobot). Only this chat may use the bot.
ALLOWED_CHAT_ID="${ALLOWED_CHAT_ID:?set ALLOWED_CHAT_ID before deploying}"

LOCAL_TZ="Asia/Kolkata"
NOTION_VERSION="2026-03-11"
NOTION_CLASS_DATA_SOURCE_ID="3aa3ef88-7ec8-8075-95b7-000b8a6396eb"
NOTION_FLOOR_DATA_SOURCE_ID="6c63ef88-7ec8-822b-b773-8786b5e160d0"

# Vertex AI (Gemini). "global" avoids per-region model availability checks.
VERTEX_PROJECT="$PROJECT_ID"
VERTEX_LOCATION="global"

# Google Docs that back the NotebookLM notebook. Created by hand in Drive and
# shared with the runtime service account as Editor — see README, "NotebookLM
# sync setup". File IDs come from docs.google.com/document/d/<FILE_ID>/edit.
DRIVE_CLASS_DOC_ID="1q8-TJqbIzRHhji9wmMwzcXP5SE8FmVmbqMsgEp6pbGY"
DRIVE_FLOOR_DOC_ID="1Iu9ah3IJ1mUDIjmASLOgCoeOiGyOB3zaJttQh5tQtP0"

# The function's own runtime service account, impersonated to obtain a
# Drive-scoped token (Cloud Run's metadata token is cloud-platform only, which
# Drive rejects). Requires roles/iam.serviceAccountTokenCreator on itself.
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
DRIVE_IMPERSONATE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud functions deploy "$FUNCTION_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --gen2 \
  --runtime=python312 \
  --source=. \
  --entry-point=main \
  --trigger-http \
  --allow-unauthenticated \
  --timeout=540s \
  --memory=512Mi \
  --set-env-vars="ALLOWED_CHAT_ID=${ALLOWED_CHAT_ID},LOCAL_TZ=${LOCAL_TZ},NOTION_VERSION=${NOTION_VERSION},NOTION_CLASS_DATA_SOURCE_ID=${NOTION_CLASS_DATA_SOURCE_ID},NOTION_FLOOR_DATA_SOURCE_ID=${NOTION_FLOOR_DATA_SOURCE_ID},VERTEX_PROJECT=${VERTEX_PROJECT},VERTEX_LOCATION=${VERTEX_LOCATION},DRIVE_CLASS_DOC_ID=${DRIVE_CLASS_DOC_ID},DRIVE_FLOOR_DOC_ID=${DRIVE_FLOOR_DOC_ID},DRIVE_IMPERSONATE_SA=${DRIVE_IMPERSONATE_SA}" \
  --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest,NOTION_TOKEN=notion-token:latest"

echo
echo "Function URL (register this as the Telegram webhook):"
gcloud functions describe "$FUNCTION_NAME" \
  --project="$PROJECT_ID" --region="$REGION" --gen2 \
  --format="value(serviceConfig.uri)"
