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

gcloud functions deploy "$FUNCTION_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --gen2 \
  --runtime=python312 \
  --source=. \
  --entry-point=main \
  --trigger-http \
  --allow-unauthenticated \
  --timeout=120s \
  --memory=512Mi \
  --set-env-vars="ALLOWED_CHAT_ID=${ALLOWED_CHAT_ID},LOCAL_TZ=${LOCAL_TZ},NOTION_VERSION=${NOTION_VERSION},NOTION_CLASS_DATA_SOURCE_ID=${NOTION_CLASS_DATA_SOURCE_ID},NOTION_FLOOR_DATA_SOURCE_ID=${NOTION_FLOOR_DATA_SOURCE_ID}" \
  --set-secrets="TELEGRAM_BOT_TOKEN=telegram-bot-token:latest,TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest,NOTION_TOKEN=notion-token:latest"

echo
echo "Function URL (register this as the Telegram webhook):"
gcloud functions describe "$FUNCTION_NAME" \
  --project="$PROJECT_ID" --region="$REGION" --gen2 \
  --format="value(serviceConfig.uri)"
